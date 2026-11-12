"""A stopped rolling Bundle call must not discard its accepted checkpoint."""
import pytest
from sqlalchemy import text
import meta_research.owners.agent_runtime as ar_module

from meta_research.owners.common import OwnerConflict
from test_bundle_native_replacement import _ReplacementAwareBundle
from test_target_root_finalizer import _current_bundle_runtime
from test_target_launch_admission import _ready_launch
from test_public_bundle_stage import _accept_real_target_root_commit
from test_public_advancement_runtime_control import _confirmed_control, _execute_control
from test_stage_hard_ceiling import _signed_ceiling_evidence


def _control(runtime, run, action):
    managed = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
    foreground = runtime.owners.advancement_engine.query_foreground(managed["quest_ref"])
    command = _confirmed_control(runtime.owners.human_collaboration,
        scope_ref="quest:" + managed["quest_ref"],
        payload={"action": action, "target": {"quest_ref": managed["quest_ref"],
            "cycle_ref": managed["cycle_ref"], "question_ref": foreground["question_ref"],
            "epoch": foreground["epoch"], "target_scope": "run", "run_ref": run.run_ref}, "reason": "operator_requested"},
        key="rolling-" + action + run.attempt_ref)
    return _execute_control(runtime.owners.human_collaboration, command, "rolling-" + action + run.attempt_ref)


def test_formal_resume_stopped_bundle_keeps_native_and_graph_without_replaying_operation(tmp_path):
    provider = _ReplacementAwareBundle()
    runtime = _current_bundle_runtime(tmp_path / "stopped-bundle", bundle_skill=provider)
    try:
        graph, target, old, _, _ = _ready_launch(runtime)
        ar = runtime.owners.agent_runtime
        assert old.native_session_ref and old.execution is not None
        operation = old.primary_invocation.operation_ref + ":target-batch-1"
        ar.begin_provider_unit(unit_ref="stopped-rolling-unit", operation_ref=operation,
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
            unit_kind="bundle_review")
        ar.record_stage_provider_hard_ceiling(unit_ref="stopped-rolling-unit",
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
            failure_code="codex_operation_stopped", provider_exit=_signed_ceiling_evidence("stopped"))
        stopped = ar.query_managed_run(old.run_ref)
        assert stopped["status"] == "suspended_fenced"
        assert stopped["safe_point"]["checkpoint"]["provider_operation_retry_permitted"] is False
        assert _control(runtime, old, "resume")["executed"] is True
        resumed = ar.query_bundle_stage_run(old.request_ref)
        # Drive the real worker before inspecting the lost identity: this is the
        # production failure, not merely a private helper contract.
        runtime.bundle_stage.process_once()
        assert runtime.bundle_stage.observation.code != "bundle_native_replacement_context_missing"
        assert resumed.native_session_ref == old.native_session_ref
        assert resumed.primary_draft.draft == old.primary_draft.draft
        assert resumed.run_ref == old.run_ref and resumed.root_session_ref == old.root_session_ref
        assert resumed.attempt_ref != old.attempt_ref and resumed.fence_ref != old.fence_ref
        assert resumed.primary_invocation.operation_ref != old.primary_invocation.operation_ref
        assert ar.query_stage_native_replacement_context(run_ref=resumed.run_ref,
            attempt_ref=resumed.attempt_ref, fence_ref=resumed.fence_ref) is None
        assert runtime.owners.research_graph.query_target_graph(old.request_ref) == graph
        assert provider.primary_calls == 1
        with pytest.raises(OwnerConflict):
            ar.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=old.run_ref,
                attempt_ref=old.attempt_ref, root_session_ref=old.root_session_ref,
                fence_ref=old.fence_ref, runtime_binding_hash=old.runtime_binding_hash)
    finally:
        runtime.close()


def test_startup_recovers_only_unused_legacy_resume_from_exact_owner_ancestry(tmp_path, monkeypatch):
    provider = _ReplacementAwareBundle()
    root = tmp_path / "legacy-stopped-bundle"
    runtime = _current_bundle_runtime(root, bundle_skill=provider)
    try:
        target, graph_ref = _accept_real_target_root_commit(runtime)
        request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        assert graph.graph_ref == graph_ref
        ar = runtime.owners.agent_runtime
        original = ar.query_bundle_stage_run(request_ref)
        with runtime._database.read() as connection:
            original_commits = connection.execute(text("SELECT commit_ref,closure_hash FROM rg_target_commits")).all()
        assert len(original_commits) == 1
        old = original
        for legacy in (False, True):
            unit = "stopped-" + old.attempt_ref
            ar.begin_provider_unit(unit_ref=unit, operation_ref=old.primary_invocation.operation_ref,
                run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
                unit_kind="bundle_review")
            ar.record_stage_provider_hard_ceiling(unit_ref=unit,
                run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
                failure_code="codex_operation_stopped", provider_exit=_signed_ceiling_evidence("stopped"))
            if legacy:
                # Execute the original 8638 resume branch; no direct state edits.
                with monkeypatch.context() as patch:
                    patch.setattr(ar_module, "_stopped_stage_checkpoint", lambda *_: None)
                    assert _control(runtime, old, "resume")["executed"] is True
            else:
                assert _control(runtime, old, "resume")["executed"] is True
                assert runtime.bundle_stage.process_once()
                old = ar.query_bundle_stage_run(old.request_ref)
                assert old.execution is not None and old.native_session_ref == original.native_session_ref
        broken = ar.query_bundle_stage_run(old.request_ref)
        assert broken.native_session_ref is None and broken.primary_draft is None
        with pytest.raises(OwnerConflict, match="bundle_native_replacement_context_missing"):
            runtime.bundle_stage.process_once()
        prior_operation = broken.primary_invocation.operation_ref
        runtime.close()
        runtime = _current_bundle_runtime(root, bundle_skill=provider)
        ar = runtime.owners.agent_runtime
        recovered = ar.query_bundle_stage_run(old.request_ref)
        assert recovered.attempt_ref == broken.attempt_ref and recovered.fence_ref == broken.fence_ref
        assert recovered.native_session_ref == original.native_session_ref
        assert recovered.primary_draft.draft == original.primary_draft.draft
        assert recovered.primary_invocation.operation_ref == prior_operation
        assert recovered.primary_invocation.operation_ref != old.primary_invocation.operation_ref
        assert runtime.owners.research_graph.query_target_graph(old.request_ref) == graph
        before = ar.query_managed_run(old.run_ref)
        ar._recover_resumed_bundle_checkpoints()
        assert ar.query_managed_run(old.run_ref) == before
        assert runtime.bundle_stage.process_once()
        assert provider.primary_calls == 1
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT commit_ref,closure_hash FROM rg_target_commits")).all() == original_commits
        for retired in (original, old):
            with pytest.raises(OwnerConflict):
                ar.verify_root_agent_runtime_scope(root_kind="bundle", run_ref=retired.run_ref,
                    attempt_ref=retired.attempt_ref, root_session_ref=retired.root_session_ref,
                    fence_ref=retired.fence_ref, runtime_binding_hash=retired.runtime_binding_hash)
    finally:
        runtime.close()


@pytest.mark.parametrize("damage", ["missing-witness", "foreign-root", "new-provider", "stop-hash", "resume-receipt", "dispatch-receipt", "later-revision", "native-unavailable"])
def test_legacy_resume_recovery_refuses_missing_foreign_changed_or_used_work(tmp_path, monkeypatch, damage):
    runtime = _current_bundle_runtime(tmp_path / "rejected-legacy", bundle_skill=_ReplacementAwareBundle())
    try:
        _, _, old, _, _ = _ready_launch(runtime)
        ar = runtime.owners.agent_runtime
        ar.begin_provider_unit(unit_ref="stopped-unit", operation_ref=old.primary_invocation.operation_ref,
            run_ref=old.run_ref, attempt_ref=old.attempt_ref, fence_ref=old.fence_ref, unit_kind="bundle_review")
        ar.record_stage_provider_hard_ceiling(unit_ref="stopped-unit", run_ref=old.run_ref,
            attempt_ref=old.attempt_ref, fence_ref=old.fence_ref,
            failure_code="codex_operation_stopped", provider_exit=_signed_ceiling_evidence("stopped"))
        with monkeypatch.context() as patch:
            patch.setattr(ar_module, "_stopped_stage_checkpoint", lambda *_: None)
            assert _control(runtime, old, "resume")["executed"] is True
        broken = ar.query_bundle_stage_run(old.request_ref)
        assert broken.native_session_ref is None
        if damage == "new-provider":
            ar.begin_provider_unit(unit_ref="already-started-new-work", operation_ref=broken.primary_invocation.operation_ref,
                run_ref=broken.run_ref, attempt_ref=broken.attempt_ref, fence_ref=broken.fence_ref, unit_kind="bundle_primary")
            ar.acknowledge_provider_safe_point(unit_ref="already-started-new-work", run_ref=broken.run_ref,
                attempt_ref=broken.attempt_ref, fence_ref=broken.fence_ref)
        else:
            # Controlled corrupt/missing evidence cases, confined to this fixture.
            statements = {
                "missing-witness": "DELETE FROM ar_bundle_dispatch_decisions WHERE run_ref=:run",
                "foreign-root": "UPDATE ar_stage_attempts SET root_session_ref=(SELECT root_session_ref FROM ar_stage_runs WHERE stage='idea' LIMIT 1) WHERE attempt_ref=:old",
                "stop-hash": "UPDATE ar_safe_points SET checkpoint_hash=:bad_hash WHERE run_ref=:run",
                "resume-receipt": "UPDATE ar_control_operations SET receipt_hash=:bad_hash WHERE action='resume' AND run_ref=:run",
                "dispatch-receipt": "UPDATE ar_bundle_dispatch_decisions SET receipt_hash=:bad_hash WHERE run_ref=:run",
                "later-revision": "UPDATE ar_run_controls SET control_revision=control_revision+1 WHERE run_ref=:run",
                "native-unavailable": "UPDATE ar_stage_attempt_replacements SET reason_code='native_session_unavailable' WHERE replacement_attempt_ref=:current",
            }
            with runtime._database.write() as connection:
                connection.execute(text(statements[damage]), {"run": old.run_ref, "old": old.attempt_ref,
                    "current": broken.attempt_ref, "bad_hash": "f" * 64})
        with runtime._database.read() as connection:
            before = dict(connection.execute(text("SELECT * FROM ar_stage_attempts WHERE attempt_ref=:ref"),
                {"ref": broken.attempt_ref}).one()._mapping)
        ar._recover_resumed_bundle_checkpoints()
        with runtime._database.read() as connection:
            assert dict(connection.execute(text("SELECT * FROM ar_stage_attempts WHERE attempt_ref=:ref"),
                {"ref": broken.attempt_ref}).one()._mapping) == before
            assert connection.execute(text("SELECT native_session_ref FROM ar_stage_sessions WHERE session_ref=:ref"),
                {"ref": broken.root_session_ref}).scalar_one() is None
    finally:
        runtime.close()
