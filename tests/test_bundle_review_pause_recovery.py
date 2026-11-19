"""Resume a Bundle whose rolling provider stopped while HC was applying pause."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import text

import meta_research.owners.agent_runtime as ar_module
from meta_research.bundle_skill import BundleSkillUnavailable
from meta_research.owners.common import OwnerConflict
from test_bundle_no_action_coordination import _NoActionBundle
from test_public_advancement_runtime_control import (
    _confirmed_control, _drop_control_execution_receipt, _execute_control,
)
from test_public_bundle_stage import _accept_real_target_root_commit
from test_stage_hard_ceiling import _signed_ceiling_evidence
from test_target_root_finalizer import _current_bundle_runtime


class _PausingReview(_NoActionBundle):
    def __init__(self):
        super().__init__("wait")
        self.started = Event()
        self.stop = Event()
        self.stopped_job = None
        self.primary_calls = 0
        self.new_work = True

    def generate_draft(self, request):
        self.primary_calls += 1
        return super().generate_draft(request)

    def propose_target_batch(self, request):
        if self.started.is_set():
            return super().propose_target_batch(request)
        self.stopped_job = request.job_ref
        self.started.set()
        assert self.stop.wait(15), "HC did not stop the rolling review"
        raise BundleSkillUnavailable(
            "codex_operation_stopped", recovery_checkpoint=_signed_ceiling_evidence("stopped")
        )

    def cancel_job(self, job_ref):
        assert job_ref == self.stopped_job
        self.stop.set()

    def reconcile_cancelled_job(self, job_ref):
        # The worker records its hard ceiling while the pause operation waits.
        return False


def _command(runtime, quest, action, key):
    head = runtime.owners.advancement_engine.query_foreground(quest)
    return _confirmed_control(
        runtime.owners.human_collaboration, scope_ref=f"quest:{quest}", key=key,
        payload={"action": action, "reason": "operator_requested",
                 "target": {k: head[k] for k in ("quest_ref", "cycle_ref", "question_ref", "epoch")}},
    )


def _attempt_rows(runtime, run):
    with runtime._database.read() as connection:
        return connection.execute(text(
            "SELECT a.attempt_ref, a.fence_ref, a.generation, f.status "
            "FROM ar_stage_attempts a JOIN ar_execution_fences f ON a.fence_ref=f.fence_ref "
            "WHERE a.run_ref=:ref ORDER BY a.generation"
        ), {"ref": run.run_ref}).all()


@pytest.mark.parametrize("history,damage", [
    ("first-pause", None),
    ("legacy-resume-pause", None),
    ("legacy-resume-pause", "stop-checkpoint"),
    ("legacy-resume-pause", "resume-receipt"),
])
def test_resume_rolling_review_from_authenticated_pause_history(tmp_path, monkeypatch, history, damage):
    provider = _PausingReview()
    root = tmp_path / "rolling-review"
    runtime = _current_bundle_runtime(root, bundle_skill=provider)
    try:
        target, graph_ref = _accept_real_target_root_commit(runtime)
        request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
        owner = runtime.owners.agent_runtime
        old = owner.query_bundle_stage_run(request_ref)
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        commits = runtime.owners.research_graph.query_target_commits(graph_ref)
        assert [c.target_ref for c in commits] == [target.target_ref]
        assert old.native_session_ref and old.execution is not None

        def drive_stopped_review():
            for _ in range(12):
                runtime.bundle_stage.process_once()
                if provider.started.is_set():
                    return runtime.bundle_stage.observation.code
            pytest.fail("no rolling review after the accepted Target commit")

        human = runtime.owners.human_collaboration
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(drive_stopped_review)
            try:
                assert provider.started.wait(15), "provider did not enter the rolling job"
                pause = _command(runtime, graph.quest_ref, "pause", "review-pause")
                assert _execute_control(human, pause, "review-pause")["executed"]
            finally:
                provider.stop.set()
            assert worker.result(timeout=15) == "codex_operation_stopped"
        paused = owner.query_managed_run(old.run_ref)
        assert paused["status"] == "suspended"
        assert paused["safe_point"]["checkpoint"]["run_status"] == "suspended_fenced"
        assert _attempt_rows(runtime, old) == [(old.attempt_ref, old.fence_ref, 1, "rejected")]

        if history == "legacy-resume-pause":
            # R16 completed this resume without checking the earlier stopped review.
            with monkeypatch.context() as legacy:
                legacy.setattr(ar_module, "_paused_bundle_stopped_checkpoint", lambda *_: None, raising=False)
                command = _command(runtime, graph.quest_ref, "resume", "legacy-resume")
                assert _execute_control(human, command, "legacy-resume")["executed"]
            assert owner.query_managed_run(old.run_ref)["status"] == "running"
            with pytest.raises(OwnerConflict, match="target_frontier_integrity_invalid|runtime_fence_revoked"):
                runtime.bundle_stage.process_once()
            with runtime._database.read() as connection:
                stopped_unit = connection.execute(text(
                    "SELECT unit_ref FROM ar_provider_units WHERE run_ref=:run AND status='revoked'"
                ), {"run": old.run_ref}).scalar_one()
            with pytest.raises(OwnerConflict, match="runtime_fence_revoked"):
                owner.begin_provider_unit(unit_ref=stopped_unit, operation_ref=provider.stopped_job,
                    unit_kind="bundle_review", run_ref=old.run_ref,
                    attempt_ref=old.attempt_ref, fence_ref=old.fence_ref)
            second = _command(runtime, graph.quest_ref, "pause", "legacy-repause")
            assert _execute_control(human, second, "legacy-repause")["executed"]
            assert owner.query_managed_run(old.run_ref)["safe_point"]["checkpoint"]["run_status"] == "running"

        runtime.close()
        runtime = _current_bundle_runtime(root, bundle_skill=provider)
        owner = runtime.owners.agent_runtime
        human = runtime.owners.human_collaboration
        command = _command(runtime, graph.quest_ref, "resume", "recover-review")
        if damage:
            # Corrupt only a historical proof in this isolated test database.
            statement = (
                "UPDATE ar_safe_points SET checkpoint_hash=:bad WHERE run_ref=:run "
                "AND json_extract(checkpoint_json,'$.action')='provider_hard_ceiling'"
                if damage == "stop-checkpoint" else
                "UPDATE ar_control_operations SET receipt_hash=:bad WHERE action='resume'"
            )
            with runtime._database.write() as connection:
                assert connection.execute(text(statement), {"run": old.run_ref, "bad": "f" * 64}).rowcount == 1
            with pytest.raises(OwnerConflict):
                _execute_control(human, command, "recover-review")
            assert _attempt_rows(runtime, old) == [(old.attempt_ref, old.fence_ref, 1, "rejected")]
            assert owner.query_managed_run(old.run_ref)["status"] == "suspended"
            return

        executed = _execute_control(human, command, "recover-review")
        assert executed["executed"]
        recovered = owner.query_bundle_stage_run(request_ref)
        assert recovered.run_ref == old.run_ref and recovered.root_session_ref == old.root_session_ref
        assert recovered.native_session_ref == old.native_session_ref
        assert recovered.primary_draft.draft_hash == old.primary_draft.draft_hash
        assert recovered.primary_invocation.operation_ref != old.primary_invocation.operation_ref
        assert recovered.attempt_ref != old.attempt_ref and recovered.fence_ref != old.fence_ref
        attempts = _attempt_rows(runtime, old)
        assert len(attempts) == 2 and attempts[0] == (old.attempt_ref, old.fence_ref, 1, "rejected")
        assert attempts[1][:3] == (recovered.attempt_ref, recovered.fence_ref, 2)
        receipts = executed["control_execution"]["owner_receipts"]
        assert _execute_control(human, command, "recover-review")["control_execution"]["owner_receipts"] == receipts
        _drop_control_execution_receipt(runtime, command["intent_id"])
        assert _execute_control(human, command, "recover-receipt")["control_execution"]["owner_receipts"] == receipts
        assert _attempt_rows(runtime, old) == attempts
        with pytest.raises(OwnerConflict):
            owner.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=old.run_ref,
                attempt_ref=old.attempt_ref, root_session_ref=old.root_session_ref,
                fence_ref=old.fence_ref, runtime_binding_hash=old.runtime_binding_hash)
        assert runtime.owners.research_graph.query_target_graph(request_ref) == graph
        for _ in range(10):
            runtime.bundle_stage.process_once()
            if provider.batches:
                break
        assert len(provider.batches) == 1 and provider.primary_calls == 1
        assert provider.batches[0].native_session_ref == old.native_session_ref
        assert provider.batches[0].job_ref != provider.stopped_job
        proposals = owner.query_bundle_target_proposals(old.run_ref)
        assert len(proposals) == 1
        proposal = proposals[0]
        assert (proposal.attempt_ref, proposal.fence_ref) == (recovered.attempt_ref, recovered.fence_ref)
        dispatch = owner.query_bundle_dispatch_decisions(old.run_ref)[0]
        checkpoint = owner.query_bundle_inbox_operation_checkpoint(
            operation_kind="dispatch", operation_ref=dispatch.decision_ref,
        )
        assert checkpoint.attempt_ref == old.attempt_ref
        with pytest.raises(OwnerConflict, match="attempt_fence_stale"):
            owner.record_bundle_target_proposal(
                run_ref=old.run_ref, attempt_ref=old.attempt_ref,
                fence_ref=old.fence_ref, native_session_ref=old.native_session_ref,
                graph_ref=graph_ref, base_generation=proposal.base_generation,
                base_head_receipt=proposal.base_head_receipt, strategy_update=proposal.strategy_update,
                inbox_checkpoint=checkpoint, idempotency_key="late-proposal-with-retired-bundle-fence",
            )
        assert owner.query_bundle_target_proposals(old.run_ref) == proposals
        assert runtime.owners.research_graph.query_target_commits(graph_ref) == commits
    finally:
        runtime.close()
