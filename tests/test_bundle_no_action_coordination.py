"""A rejected no-action batch can hand incomplete research to its root coordinator."""
from dataclasses import replace
import json
import sys

from sqlalchemy import text

import pytest
from fastapi.testclient import TestClient

from meta_research.bundle_skill import BundleDispatchResult
from meta_research.bundle_stage import _rolling_provider_unit_ref
from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from meta_research.semantic_owner_gateway import BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
from meta_research.web import create_app
from test_public_bundle_stage import _accept_real_target_root_commit, _formal_target_plan, _formal_candidate
from test_target_root_finalizer import _CurrentBindingBundleSkill, _current_bundle_runtime
from test_stage_terminal_contract_authority import _provider_evidence
from test_root_human_request_lifecycle import _initialized_child, _open_arguments, _structured, _tool_call


class _NoActionBundle(_CurrentBindingBundleSkill):
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.batches = []
        self.coordination = []
        self.new_work = False

    def _target_plan(self, request):
        cells = {brief["experiment_key"]: ("preparation:" + brief["experiment_key"],
                 "comparison:" + brief["experiment_key"]) for brief in request.plan_document["experiment_briefs"]}
        first = next(iter(cells))
        return _formal_target_plan(request, cells_by_experiment=cells,
            candidates=(("preparation-only", first, cells[first][0], (), "normal"),))

    def cancel_job(self, job_ref):
        pass

    def reconcile_cancelled_job(self, job_ref):
        # This deterministic provider has no external process/effects. The real
        # Owner still performs its original revocation and cleanup transitions.
        return True

    def propose_target_batch(self, request):
        self.batches.append(request)
        result = super().propose_target_batch(request)
        if self.new_work:
            key = request.plan_document["experiment_briefs"][0]["experiment_key"]
            candidate = _formal_candidate(completion_document=request.initial_target_plan["completion_contract"],
                label="new-comparison", experiment_key=key, cell="comparison:" + key)
            self.valid_batch = replace(result, strategy_update={**result.strategy_update,
                "candidates": [candidate], "strategy_complete": False})
            return self.valid_batch
        return replace(result, strategy_update={**result.strategy_update, "strategy_complete": False},
            rationale="The accepted preparation result leaves comparison obligations unmet; the frozen stop rule prevents another preparation attempt.")

    def terminal_contract_failure_checkpoint(self, **values):
        return _provider_evidence(failure_code=values["failure_code"], detail_code=values["detail_code"])

    def schedule_target(self, request):
        if request.frontier:
            return super().schedule_target(request)
        self.coordination.append(request)
        return BundleDispatchResult(self.action, None,
            "The accepted preparation work cannot meet remaining frozen obligations without revising the stop rule in a successor Cycle.",
            request.native_session_ref, "test_deterministic")


def _rejected_batch(tmp_path, action):
    provider = _NoActionBundle(action)
    runtime = _current_bundle_runtime(tmp_path / "no-action-batch", bundle_skill=provider)
    _accept_real_target_root_commit(runtime)
    request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
    owner = runtime.owners.agent_runtime
    before = owner.query_bundle_stage_run(request_ref)
    graph = runtime.owners.research_graph.query_target_graph(request_ref)
    commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
    assert len(graph.targets) == len(commits) == 1 and not graph.strategy_complete
    for _ in range(6):
        runtime.bundle_stage.process_once()
        if provider.batches:
            break
    assert len(provider.batches) == 1
    assert runtime.bundle_stage.observation.code == "bundle_review_result_contract_invalid"
    current = owner.query_bundle_stage_run(request_ref)
    feedback = owner.query_stage_provider_correction_feedback(run_ref=current.run_ref,
        attempt_ref=current.attempt_ref, fence_ref=current.fence_ref)
    assert feedback["detail_code"] == "strategy_update_has_no_actionable_work"
    assert current.attempt_ref != before.attempt_ref
    assert current.run_ref == before.run_ref and current.root_session_ref == before.root_session_ref
    assert current.native_session_ref == before.native_session_ref
    return runtime, provider, current, graph, commits, feedback


def _system_help_roundtrip(runtime, run, provider, *, restart, primary_pending=False):
    owner = runtime.owners.agent_runtime
    while not primary_pending and run.execution is None:
        assert runtime.bundle_stage.process_once()
        run = owner.query_bundle_stage_run(run.request_ref)
    batch_unit = (run.primary_invocation.invocation_ref if primary_pending else
        _rolling_provider_unit_ref(operation_ref=run.primary_invocation.operation_ref,
            operation_name="target-batch-1", attempt_ref=run.attempt_ref))
    owner.begin_provider_unit(unit_ref=batch_unit, operation_ref=run.primary_invocation.operation_ref,
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref, unit_kind="bundle_primary" if primary_pending else "bundle_review")
    channel = runtime.harnesses.issue_resident_mcp_channel(run_ref=run.run_ref,
        attempt_ref=run.attempt_ref, root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
        capability_binding_hash=run.runtime_binding_hash, operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
        root_kind="bundle", phase="primary" if primary_pending else "target-batch-1", subject_policy="operation_tree")
    with TestClient(create_app(runtime, base_url="http://testserver", control_key="control-key"),
                    base_url="http://testserver") as client:
        headers = _initialized_child(client, channel.connection.token)
        request = _structured(_tool_call(client, headers, operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
            arguments=_open_arguments("no-action-coordination-route", "system_operation_help"), request_id=20))
        assert request["operation_binding"]["task_ref"] == run.run_ref
        assert owner.query_managed_run(run.run_ref)["status"] == "suspended"
    if restart:
        root = runtime.data_root.root
        runtime.close()
        runtime = _current_bundle_runtime(root, bundle_skill=provider)
        owner = runtime.owners.agent_runtime
    else:
        owner.acknowledge_provider_safe_point(unit_ref=batch_unit, run_ref=run.run_ref,
            attempt_ref=run.attempt_ref, fence_ref=run.fence_ref)
    runtime.owners.human_collaboration.respond_to_human_request(request["request_ref"],
        decision="provided", facts={"action": "retry"},
        note="Implementer-filled engineering fact: coordinator route available; no research rule or budget changed.",
        idempotency_key="retry-no-action-coordination-route")
    answered = owner.query_human_request(request["request_ref"])
    assert answered["status"] == "satisfied"
    assert answered["direct_waiters"][0]["status"] == "consumed"
    assert owner.query_managed_run(run.run_ref)["status"] == "running"
    resumed = owner.query_bundle_stage_run(run.request_ref)
    assert resumed.root_session_ref == run.root_session_ref and resumed.native_session_ref == run.native_session_ref
    if restart:
        assert resumed.attempt_ref != run.attempt_ref
        if primary_pending:
            assert resumed.primary_invocation.operation_ref == run.primary_invocation.operation_ref
            assert resumed.primary_draft is None
        else:
            assert resumed.primary_invocation.operation_ref != run.primary_invocation.operation_ref
            assert resumed.primary_draft.draft == run.primary_draft.draft
            assert resumed.primary_draft.draft_hash == run.primary_draft.draft_hash
            assert resumed.primary_invocation.response_hash == run.primary_invocation.response_hash
    else:
        assert resumed.attempt_ref == run.attempt_ref
    return runtime, resumed



@pytest.mark.parametrize("action,restart", [("replan_required", False), ("wait", False), ("replan_required", True)], ids=["replan", "wait", "restart-hr"])
def test_no_action_correction_reaches_exact_root_coordinator_and_preserves_unmet_work(tmp_path, action, restart):
    runtime, provider, run, graph, commits, feedback = _rejected_batch(tmp_path, action)
    try:
        if action == "replan_required":
            runtime, run = _system_help_roundtrip(runtime, run, provider, restart=restart)
        owner = runtime.owners.agent_runtime
        assert owner.query_stage_provider_correction_feedback(run_ref=run.run_ref,
            attempt_ref=run.attempt_ref, fence_ref=run.fence_ref) == feedback
        for _ in range(12):
            runtime.bundle_stage.process_once()
            if provider.coordination:
                break
        assert len(provider.coordination) == 1, "no-action batch never reached the existing empty-frontier coordinator"
        request = provider.coordination[0]
        assert request.frontier == () and request.provider_feedback == feedback
        assert (request.run_ref, request.attempt_ref, request.root_session_ref, request.fence_ref, request.native_session_ref) == (
            run.run_ref, run.attempt_ref, run.root_session_ref, run.fence_ref, run.native_session_ref)
        assert request.runtime_binding == run.runtime_binding
        assert len(provider.batches) == 1
        for _ in range(8):
            runtime.bundle_stage.process_once()
            if runtime.owners.advancement_engine.query_bundle_stage_commit(run.request_ref) is not None:
                break
        assert runtime.owners.research_graph.query_target_graph(run.request_ref) == graph
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
        assert len(provider.coordination) == 1 and len(provider.batches) == 1
        decision = owner.query_bundle_dispatch_decisions(run.run_ref)[-1]
        assert decision.action == action and decision.selected_target_ref is None and decision.frontier == ()
        accepted = owner.query_bundle_run_report(run.run_ref)
        stage_commit = runtime.owners.advancement_engine.query_bundle_stage_commit(run.request_ref)
        if action == "replan_required":
            assert accepted is not None and accepted.report.disposition == "replan_required"
            assert stage_commit is not None
            assert accepted.report.accepted_target_commit_refs == (commits[0].commit_ref,)
            assert accepted.report.semantic_change_required == (decision.rationale,)
            assert accepted.report.remaining_experiment_keys
        else:
            assert accepted is None and stage_commit is None
            assert owner.query_managed_run(run.run_ref)["status"] == "running"
        with pytest.raises(OwnerConflict):
            owner.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=run.run_ref,
                attempt_ref=feedback["rejected_attempt_ref"], root_session_ref=run.root_session_ref,
                fence_ref=provider.batches[0].fence_ref, runtime_binding_hash=run.runtime_binding_hash)
    finally:
        runtime.close()


@pytest.mark.parametrize("restart", [False, True], ids=["current-root", "hr-restart"])
def test_wait_preserves_batch_opportunity_after_new_inbox_generation(tmp_path, restart):
    runtime, provider, run, graph, commits, feedback = _rejected_batch(tmp_path, "wait")
    try:
        prior = run
        if restart:
            runtime, run = _system_help_roundtrip(runtime, run, provider, restart=True)
        for _ in range(12):
            runtime.bundle_stage.process_once()
            if provider.coordination:
                break
        assert len(provider.coordination) == 1
        for _ in range(3):
            runtime.bundle_stage.process_once()
        assert len(provider.coordination) == len(provider.batches) == 1
        # Controlled Owner-input change: advance only the scoped inbox cut.
        # This models newly published facts without fabricating a Target result.
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE ar_bundle_inbox_scopes SET generation=generation+1, "
                "current_checkpoint_ref=NULL, wake_pending=1 WHERE run_ref=:run"), {"run": run.run_ref})
        provider.new_work = True
        for _ in range(6):
            runtime.bundle_stage.process_once()
            current = runtime.owners.research_graph.query_target_graph(run.request_ref)
            if current.head_generation > graph.head_generation:
                break
        assert len(provider.coordination) == 1 and len(provider.batches) == 2
        assert len(current.targets) == 2 and not current.strategy_complete
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
        if restart:
            # Production adapter contract: the original sealed no-action batch
            # stays immutable while the new technical job can propose real work.
            from meta_research.idea_skill import IdeaSkillUnavailable
            from test_bundle_input_drift_recovery import _adapter
            from test_bundle_skill_adapter import _inbox_checkpoint
            output = {"strategy_update": provider.valid_batch.strategy_update,
                      "rationale": provider.valid_batch.rationale}
            adapter_root = tmp_path / "real-adapter"
            adapter_root.mkdir()
            adapter, transport = _adapter(adapter_root, output, durable=True,
                native_session_ref=run.native_session_ref)
            rejected = {"strategy_update": {**provider.valid_batch.strategy_update, "candidates": []},
                        "rationale": "The original batch has no actionable work."}
            transport._outputs = iter([rejected, output])
            binding = adapter.runtime_binding()
            original_request = replace(provider.batches[0], attempt_ref=prior.attempt_ref,
                fence_ref=prior.fence_ref, job_ref=prior.primary_invocation.operation_ref,
                runtime_binding=binding, inbox_checkpoint=_inbox_checkpoint(run_ref=prior.run_ref,
                    attempt_ref=prior.attempt_ref, fence_ref=prior.fence_ref))
            with pytest.raises(IdeaSkillUnavailable, match="bundle_review_result_contract_invalid"):
                adapter.propose_target_batch(original_request)
            original_directory = next(adapter._workspace.glob("provider-operations/*/target-batch-1"))
            original_bytes = {p.name: p.read_bytes() for p in original_directory.iterdir() if p.is_file()}
            new_request = replace(provider.batches[-1], runtime_binding=binding)
            assert new_request.job_ref != original_request.job_ref
            accepted = adapter.propose_target_batch(new_request)
            assert accepted.strategy_update == provider.valid_batch.strategy_update
            assert len(transport.calls) == 2
            assert {name: (original_directory / name).read_bytes() for name in original_bytes} == original_bytes
    finally:
        runtime.close()


def test_hr_restart_cleanup_requires_signed_original_stop_and_releases_runner_latch(tmp_path, monkeypatch):
    from meta_research.bundle_skill import CodexBundleSkillAdapter
    from meta_research.quest_drafting import _CancellableProcessRunner, _ProcessStopped
    from test_idea_skill_contract import _fake_codex_executable

    runtime, provider, run, _graph, _commits, _feedback = _rejected_batch(tmp_path, "replan_required")
    try:
        old_job = run.primary_invocation.operation_ref
        runtime, resumed = _system_help_roundtrip(runtime, run, provider, restart=True)
        owner = runtime.owners.agent_runtime
        runner = _CancellableProcessRunner()
        adapter = CodexBundleSkillAdapter(tmp_path / "signed-provider",
            executable=str(_fake_codex_executable(tmp_path / "fake-codex", read_all_input=True)),
            process_runner=runner)
        # Real durable transport with a deterministic executable, no model.
        # The executable writes a fixed empty object accepted by this schema.
        executable = tmp_path / "fake-codex"
        code = executable.read_text()
        begin = code.index("result_path.write_text(")
        end = code.index("\n", begin)
        executable.write_text(code[:begin] + "result_path.write_text('{}', encoding='utf-8')" + code[end:])
        adapter._invoke(operation_name="target-batch-1", prompt="controlled closure fixture",
            schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            native_session_ref=None, job_ref=old_job)
        operation = next((tmp_path / "signed-provider/provider-operations").glob("*/target-batch-1"))
        receipt = operation / "supervisor-exit.json"
        original = receipt.read_bytes()
        envelope = json.loads(original)
        envelope["seal"] = "f" * 64
        receipt.write_text(json.dumps(envelope))
        monkeypatch.setattr(provider, "reconcile_cancelled_job", adapter.reconcile_cancelled_job)
        monkeypatch.setattr(provider, "finish_job", adapter.finish_job, raising=False)
        assert not owner.reconcile_pending_provider_cleanup(provider, unit_kinds=("bundle_review",))
        assert owner.query_managed_run(run.run_ref)["cleanup_status"] == "pending"
        assert provider.coordination == []
        with pytest.raises(_ProcessStopped):
            runner.run_job(old_job, [sys.executable, "-c", "print('next phase')"], "", 2)
        with pytest.raises(OwnerConflict, match="runtime_quiescence_requested"):
            owner.begin_provider_unit(unit_ref="blocked-new-dispatch", operation_ref=resumed.primary_invocation.operation_ref,
                run_ref=resumed.run_ref, attempt_ref=resumed.attempt_ref, fence_ref=resumed.fence_ref, unit_kind="bundle_review")
        receipt.write_bytes(original)
        def failed_release(_job):
            raise RuntimeError("controlled release failure")
        monkeypatch.setattr(provider, "finish_job", failed_release)
        with pytest.raises(RuntimeError, match="controlled release failure"):
            owner.reconcile_pending_provider_cleanup(provider, unit_kinds=("bundle_review",))
        assert owner.query_managed_run(run.run_ref)["cleanup_status"] == "pending"
        monkeypatch.setattr(provider, "finish_job", adapter.finish_job)
        assert owner.reconcile_pending_provider_cleanup(provider, unit_kinds=("bundle_review",))
        assert owner.query_managed_run(run.run_ref)["cleanup_status"] == "completed"
        assert runner.run_job(old_job, [sys.executable, "-c", "print('next phase')"], "", 2).stdout.strip() == "next phase"
        assert receipt.read_bytes() == original
        for _ in range(12):
            runtime.bundle_stage.process_once()
            if provider.coordination:
                break
        assert len(provider.coordination) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("damage", ["other-reason", "generation-gap", "foreign-root", "stale-scope"])
def test_hr_feedback_inheritance_rejects_nonexact_technical_ancestry(tmp_path, damage):
    runtime, provider, run, _graph, _commits, _feedback = _rejected_batch(tmp_path, "wait")
    try:
        runtime, resumed = _system_help_roundtrip(runtime, run, provider, restart=True)
        owner = runtime.owners.agent_runtime
        if damage == "stale-scope":
            assert owner.query_stage_provider_correction_feedback(run_ref=run.run_ref,
                attempt_ref=run.attempt_ref, fence_ref=run.fence_ref) is None
            return
        with runtime._database.write() as connection:
            if damage == "other-reason":
                connection.execute(text("UPDATE ar_stage_attempt_replacements SET reason_code='runtime_fence_replaced' "
                    "WHERE replacement_attempt_ref=:attempt"), {"attempt": resumed.attempt_ref})
            elif damage == "generation-gap":
                connection.execute(text("UPDATE ar_stage_attempts SET generation=generation+1 WHERE attempt_ref=:attempt"),
                    {"attempt": resumed.attempt_ref})
            else:
                foreign = connection.execute(text("SELECT root_session_ref FROM ar_stage_runs WHERE stage='idea' LIMIT 1")).scalar_one()
                connection.execute(text("UPDATE ar_stage_attempts SET root_session_ref=:root WHERE attempt_ref=:attempt"),
                    {"root": foreign, "attempt": resumed.attempt_ref})
        if damage == "other-reason":
            assert owner.query_stage_provider_correction_feedback(run_ref=resumed.run_ref,
                attempt_ref=resumed.attempt_ref, fence_ref=resumed.fence_ref) is None
        else:
            with pytest.raises(OwnerConflict):
                owner.query_stage_provider_correction_feedback(run_ref=resumed.run_ref,
                    attempt_ref=resumed.attempt_ref, fence_ref=resumed.fence_ref)
        assert provider.coordination == []
    finally:
        runtime.close()


def test_hr_restart_does_not_replace_an_unfinished_primary_operation(tmp_path):
    from test_public_bundle_stage import _prepare_bundle_request
    provider = _NoActionBundle("wait")
    runtime = _current_bundle_runtime(tmp_path / "pending-primary", bundle_skill=provider)
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()
        request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert run.primary_draft is None and run.primary_invocation.status == "prepared"
        runtime, resumed = _system_help_roundtrip(runtime, run, provider, restart=True, primary_pending=True)
        assert resumed.primary_invocation.operation_ref == run.primary_invocation.operation_ref
        assert runtime.owners.agent_runtime.query_managed_run(run.run_ref)["cleanup_status"] == "pending"
        assert provider.batches == provider.coordination == []
    finally:
        runtime.close()
