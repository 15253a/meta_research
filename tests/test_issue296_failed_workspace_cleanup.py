import sys
import time

import pytest

from sqlalchemy import text

from meta_research.harness import _harness_runtime_effect
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessSupervisorTransport
from meta_research.provider_supervisor import read_transport_envelope, write_transport_envelope
from meta_research.owners.common import canonical_hash
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.runtime_protection import RuntimeEffectIdentity, record_runtime_boundary
from test_completed_workspace_cleanup import (
    _cleanup_runtime, _complete, _confirm_deepfetch_quest, _finish_plan_stage,
    _finish_stage, _next_foreground, _ready_existing, _target_cleanup,
)
from test_target_root_finalizer import _admit_independent_target_root


def _failed_workspace(tmp_path):
    runtime = _cleanup_runtime(tmp_path / "failed-cleanup")
    quest = _confirm_deepfetch_quest(runtime)
    _finish_stage(runtime, "idea")
    _finish_plan_stage(runtime)
    target, candidate, plan, admission, handle = _admit_independent_target_root(
        runtime, ready=_ready_existing(runtime))
    launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
    runtime.target_root_lifecycle.activate(
        launch_ref=launch.launch_ref, handle=handle, candidate=candidate, formal_plan=plan,
        idempotency_key="issue296-failed-cleanup-activate")
    workspace_ref, workspace = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
        target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
        root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
        fence_ref=handle.execution_fence_ref)
    (workspace / "scratch.txt").write_text("Unfinished local experiment notes.")
    owner = runtime.owners.agent_runtime.harness_runs
    operation_ref = "issue296-failed-cleanup-native"
    owner_hash = "a" * 64
    owner.start_operation(run_ref=handle.target_run_ref, operation_ref=operation_ref,
        generation=1, invocation_hash=owner_hash, resume=False)
    provider_effect = _harness_runtime_effect(
        run_ref=handle.target_run_ref, attempt_ref=handle.execution_attempt_ref,
        fence_ref=handle.execution_fence_ref, operation_ref=operation_ref,
        target_root=True, reconciling=False)
    runtime.harnesses._runtime_protection.acquire(provider_effect)
    transport_root = runtime.harnesses._target_raw_output_store._workspace
    transport = HarnessSupervisorTransport(transport_root)
    runtime.harnesses._adapters["codex"] = CodexHarnessAdapter(transport_root, runner=transport)
    completed = transport([sys.executable, "-c", "import sys; sys.stdin.read(); sys.exit(23)"],
        "isolated failed workspace cleanup", 4, {
            "META_RESEARCH_HARNESS_FAMILY": "codex",
            "META_RESEARCH_PROVIDER_OPERATION_REF": operation_ref,
            "META_RESEARCH_OWNER_INVOCATION_HASH": owner_hash,
            "META_RESEARCH_HARNESS_WORKSPACE": str(workspace),
        })
    assert completed.returncode == 23
    binding_path = transport_root / "owner-invocation-bindings" / owner_hash[:2] / (owner_hash + ".json")
    binding = read_transport_envelope(binding_path, transport._transport_key)
    binding_path.unlink()
    owner.record_operation_failure(operation_ref, "provider_terminal_proof_missing",
        durable_outcome="unknown", expected_running_run_ref=handle.target_run_ref)
    runtime.target_root_lifecycle.request_cancel(handle.target_ref, reason="End unresolved local turn")
    runtime.target_root_lifecycle.mark_failed_unknown(target_ref=handle.target_ref)
    notice = runtime.owners.agent_runtime.query_target_work_notice(handle.target_ref)
    with runtime._database.read() as connection:
        stage = connection.execute(text("SELECT s.* FROM ar_stage_runs s JOIN ar_bundle_inbox_entries e "
            "ON e.run_ref=s.run_ref WHERE e.notice_ref=:notice"), {"notice": notice.notice_ref}).one()
    scope = {"run_ref": stage.run_ref, "attempt_ref": stage.current_attempt_ref,
        "fence_ref": stage.current_fence_ref}
    return runtime, handle, workspace, notice, scope, transport, binding_path, binding, quest


def _acknowledge(runtime, notice, scope):
    owner = runtime.owners.agent_runtime
    batch = owner.read_bundle_inbox(**scope)
    assert notice in batch.notices
    return owner.acknowledge_bundle_inbox(**scope, batch=batch,
        idempotency_key="issue296-failed-cleanup-ack")


def _reconcile(runtime, handle, transport, binding_path, binding):
    write_transport_envelope(binding_path, binding, transport._transport_key)
    assert runtime.target_run_runtime.process_once(handle.target_ref)
    operation = runtime.owners.agent_runtime.harness_runs.latest_operation(handle.target_run_ref)
    assert (operation.status, operation.outcome_code) == ("failed", "provider_terminal_reconciled")


def _closed_cycle_fixture(runtime, quest, scope):
    """Construct ended stage/Cycle history, without asserting scientific closure.

    The production blocked Bundle does not become a scientific completion merely
    because its failed provider was reaped. Only that separate historical metadata
    is constructed here. Target operations, receipts, handoffs, ACKs and runtime
    responsibilities are never changed by this fixture.
    """
    now = time.time()
    with runtime._database.fenced_write() as connection:
        original_operations = connection.execute(text("SELECT * FROM ar_harness_provider_operations")).all()
        original_effects = connection.execute(text("SELECT * FROM ar_execution_responsibilities")).all()
        stage = connection.execute(text("SELECT * FROM ar_stage_runs WHERE run_ref=:run"),
            {"run": scope["run_ref"]}).one()
        assert connection.execute(text("SELECT count(*) FROM ar_execution_responsibilities "
            "WHERE root_run_ref=:run AND status!='finished'"), {"run": stage.run_ref}).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM ar_provider_units WHERE run_ref=:run "
            "AND status IN ('active','revocation_pending')"), {"run": stage.run_ref}).scalar_one() == 0
        connection.execute(text("INSERT INTO ae_stage_commits (commit_ref,request_ref,cycle_ref,stage,epoch,"
            "run_ref,outcome_ref,outcome_kind,disposition,run_completion_receipt_ref,run_completion_receipt_hash,"
            "outcome_receipt_ref,outcome_receipt_hash,closure_json,closure_hash,basis_kind,basis_ref,"
            "basis_receipt_issuer,basis_receipt_kind,basis_receipt_subject_ref,basis_receipt_ref,basis_receipt_hash,"
            "idempotency_key,request_hash,receipt_ref,receipt_hash,committed_at) VALUES "
            "(:ref,:request,:cycle,'bundle',:epoch,:run,NULL,NULL,'exhausted',:ref,:hash,NULL,NULL,NULL,NULL,"
            "'isolated_closed_cycle_fixture',:ref,'fixture','fixture',:ref,:ref,:hash,:ref,:hash,:ref,:hash,:now)"),
            {"ref": "issue296-fixture-closed-bundle", "request": stage.request_ref,
             "cycle": stage.cycle_ref, "epoch": stage.epoch, "run": stage.run_ref,
             "hash": canonical_hash({"fixture": "closed-cycle-only"}), "now": now})
        connection.execute(text("UPDATE ar_run_controls SET status='terminated',cleanup_status='completed' "
            "WHERE run_ref=:run"), {"run": stage.run_ref})
        connection.execute(text("UPDATE ar_stage_sessions SET status='completed' WHERE run_ref=:run"),
            {"run": stage.run_ref})
        connection.execute(text("UPDATE ar_execution_fences SET status='rejected',closed_at=:now "
            "WHERE run_ref=:run AND status IN ('current','submitted')"), {"run": stage.run_ref, "now": now})
        connection.execute(text("UPDATE ae_cycles SET status='completed' WHERE cycle_ref=:cycle"),
            {"cycle": quest["cycle_ref"]})
        assert connection.execute(text("SELECT count(*) FROM rg_target_commits")).scalar_one() == 0
        assert connection.execute(text("SELECT * FROM ar_harness_provider_operations")).all() == original_operations
        assert connection.execute(text("SELECT * FROM ar_execution_responsibilities")).all() == original_effects


def test_failed_workspace_reclaimed_after_receipt_and_bundle_ack(tmp_path):
    runtime, handle, workspace, notice, scope, transport, binding_path, binding, quest = _failed_workspace(tmp_path)
    try:
        _acknowledge(runtime, notice, scope)
        _reconcile(runtime, handle, transport, binding_path, binding)
        _closed_cycle_fixture(runtime, quest, scope)
        _next_foreground(runtime)
        _next_foreground(runtime)
        _next_foreground(runtime)
        report = _target_cleanup(runtime, dry_run=False)
        assert len(report) == 1 and report[0]["action"] == "removed", report
        assert not workspace.exists()
        assert _target_cleanup(runtime, dry_run=False)[0]["reason"] == "already_removed"
    finally:
        runtime.close()


@pytest.mark.parametrize("dependency", ["unknown", "unacknowledged", "damaged_ack",
    "provider_hold", "external_hold", "lost_receipt"])
def test_expired_failed_workspace_does_not_wait_for_old_dependency_records(tmp_path, dependency):
    runtime, handle, workspace, notice, scope, transport, binding_path, binding, quest = _failed_workspace(tmp_path)
    try:
        checkpoint = None
        if dependency != "unacknowledged":
            checkpoint = _acknowledge(runtime, notice, scope)
        if dependency == "provider_hold":
            write_transport_envelope(binding_path, binding, transport._transport_key)
            receipt = transport.recover_transport_receipt("issue296-failed-cleanup-native",
                family="codex", owner_invocation_hash="a" * 64)
            runtime.owners.agent_runtime.harness_runs.reconcile_failed_target_operation(
                "issue296-failed-cleanup-native", receipt)
        elif dependency != "unknown":
            _reconcile(runtime, handle, transport, binding_path, binding)
        if dependency == "external_hold":
            external = RuntimeEffectIdentity("issue296-cleanup-external", "agent_runtime",
                handle.target_run_ref, "issue296-external-original", "acquisition",
                handle.execution_attempt_ref, handle.execution_fence_ref)
            runtime.harnesses._runtime_protection.acquire(external)
        if dependency == "lost_receipt":
            binding_path.unlink()
        if dependency == "damaged_ack":
            with runtime._database.fenced_write() as connection:
                connection.execute(text("UPDATE ar_bundle_inbox_checkpoints SET receipt_hash=:hash "
                    "WHERE checkpoint_ref=:ref"), {"hash": "0" * 64, "ref": checkpoint.checkpoint_ref})
        _closed_cycle_fixture(runtime, quest, scope)
        for _ in range(3):
            _next_foreground(runtime)
        report = _target_cleanup(runtime, dry_run=False)
        assert len(report) == 1 and report[0]['action'] == 'removed', report
        assert not workspace.exists()
    finally:
        runtime.close()


def test_failed_workspace_window_and_linked_original_survive_repeated_cleanup(tmp_path):
    runtime, handle, workspace, notice, scope, transport, binding_path, binding, quest = _failed_workspace(tmp_path)
    try:
        _acknowledge(runtime, notice, scope)
        _reconcile(runtime, handle, transport, binding_path, binding)
        original = workspace / "preserved" / "source" / "raw.csv"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"sample,value\na,23\n")
        inode = original.stat().st_ino
        asset = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="local_path", custody_mode="linked_local", display_name="raw.csv",
            source_locator=str(original)), idempotency_key="issue296-failed-gc-original").asset
        assert asset is not None
        handoff = runtime.owners.agent_runtime.read_target_run_handoff(notice.handoff_manifest_ref)
        _closed_cycle_fixture(runtime, quest, scope)
        _next_foreground(runtime, resume=True)
        for _ in range(3):
            report = _target_cleanup(runtime, dry_run=False)
            assert report[0]["reason"] == "recent_cycle_window" and workspace.is_dir(), report
            _next_foreground(runtime)
        for _ in range(2):
            released = _target_cleanup(runtime, dry_run=False)
            assert (released[0]["action"], released[0]["reason"]) == ("pruned", "originals_preserved_in_place"), released
            assert not (workspace / "scratch.txt").exists()
            assert original.stat().st_ino == inode
            assert runtime.owners.research_memory.materialize_asset(asset.version_ref).content == b"sample,value\na,23\n"
            assert runtime.owners.agent_runtime.read_target_run_handoff(notice.handoff_manifest_ref) == handoff
    finally:
        runtime.close()


def test_reconciled_failed_target_remains_a_technical_bundle_blocker(tmp_path):
    runtime, handle, workspace, notice, scope, transport, binding_path, binding, quest = _failed_workspace(tmp_path)
    try:
        _acknowledge(runtime, notice, scope)
        _reconcile(runtime, handle, transport, binding_path, binding)
        for _ in range(8):
            if not runtime.bundle_stage.process_once():
                break
        report = runtime.owners.agent_runtime.query_bundle_run_report(scope["run_ref"])
        assert report is not None and report.report.disposition == "blocked"
        disposition = runtime.owners.advancement_engine.query_bundle_report_disposition(report.report_ref)
        assert disposition is not None and disposition.disposition == "blocked"
        request = runtime.owners.advancement_engine.query_bundle_stage_request(quest["cycle_ref"])
        assert runtime.owners.advancement_engine.query_bundle_stage_commit(request.request_ref) is None
        assert runtime.owners.agent_runtime.query_target_work_notice(handle.target_ref) == notice
        assert workspace.is_dir()
    finally:
        runtime.close()


def test_expired_completed_workspace_does_not_wait_for_external_responsibility(tmp_path):
    runtime, handle, _, workspace, _, _, _ = _complete(tmp_path)
    try:
        external = RuntimeEffectIdentity("issue296-completed-cleanup-external", "agent_runtime",
            handle.target_run_ref, "issue296-completed-external-original", "acquisition",
            handle.execution_attempt_ref, handle.execution_fence_ref)
        runtime.harnesses._runtime_protection.acquire(external)
        report = _target_cleanup(runtime, dry_run=False)
        assert report[0]['action'] == 'removed', report
        assert not workspace.exists()
    finally:
        runtime.close()


def test_failed_workspace_cleanup_preserves_another_quests_active_root(tmp_path):
    from meta_research.semantic_mcp import SemanticCallContext
    from test_historical_research import _another_quest
    from test_idea_owner_integrity import _admit_direct_idea_request

    runtime, handle, workspace, notice, scope, transport, binding_path, binding, quest = _failed_workspace(tmp_path)
    try:
        _acknowledge(runtime, notice, scope)
        _reconcile(runtime, handle, transport, binding_path, binding)
        _closed_cycle_fixture(runtime, quest, scope)
        for _ in range(3):
            _next_foreground(runtime)
        other = _another_quest(runtime)
        assert other["quest_ref"] != quest["quest_ref"]
        _, request, run = _admit_direct_idea_request(runtime, other, "issue296-other-quest")
        context = SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref,
            run.fence_ref, run.runtime_binding_hash, "idea", "primary", "")
        other_workspace = runtime.root_workspaces.bind_runtime(context).directory
        (other_workspace / "current-work.txt").write_bytes(b"Another Quest remains active.")
        report = _target_cleanup(runtime, dry_run=False)
        assert report[0]["action"] == "removed" and not workspace.exists(), report
        assert (other_workspace / "current-work.txt").read_bytes() == b"Another Quest remains active."
        assert runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref) == run
        assert runtime.owners.advancement_engine.query_recent_workspace_cycles(other["quest_ref"]) == (other["cycle_ref"],)
    finally:
        runtime.close()
