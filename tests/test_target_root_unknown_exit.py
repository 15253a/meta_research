from pathlib import Path
import asyncio
import json
import sys

import pytest
from alembic import command
from sqlalchemy import text

from meta_research.harness_control import HarnessCancellationError
from meta_research.migration import upgrade_database
from meta_research.owners.common import OwnerConflict
from meta_research.owners.common import canonical_hash
from meta_research.owners.agent_runtime_harness import AgentRuntimeHarnessError
from meta_research.quest_goal import ResearchRoot
from meta_research.native_session_cleanup import _cycle_dependencies
from test_target_root_cancel import _running_root


def test_stop_without_terminal_proof_publishes_failure_and_preserves_operation(tmp_path: Path):
    runtime, handle, initial = _running_root(tmp_path)
    try:
        owner = runtime.owners.agent_runtime.harness_runs
        operation_ref = "issue-296-unknown-operation"
        owner.start_operation(run_ref=handle.target_run_ref, operation_ref=operation_ref,
                              generation=1, invocation_hash="a" * 64, resume=False)
        admission = next(value for value in runtime.harnesses._admissions_by_request.values()
                         if value.run.run_ref == handle.target_run_ref)
        old_token = admission.connection.token
        assert runtime.harnesses.external_mcp_scope_is_current(old_token)

        class LostSupervisor:
            calls = 0

            def cancel_operation(self, invocation_hash):
                assert invocation_hash == "a" * 64
                self.calls += 1
                raise HarnessCancellationError("provider_terminal_proof_missing")

        canceller = LostSupervisor()
        runtime.harnesses._operation_canceller = canceller
        runtime.target_root_lifecycle.request_cancel(handle.target_ref, reason="operator stop")

        assert runtime.target_run_runtime.process_once(handle.target_ref) is True
        failed = runtime.target_root_lifecycle.query(handle.target_ref)
        assert failed.status == "failed"
        assert failed.cancelled_at is None
        assert failed.completion_ref is None
        assert failed.cancel_reason == "operator stop"
        assert failed.root_session_ref == initial.root_session_ref
        assert runtime.target_root_lifecycle.request_cancel(
            handle.target_ref, reason="operator stop") == failed
        assert runtime.target_run_runtime.process_once(handle.target_ref) is False
        assert canceller.calls == 1
        status = runtime.target_run_runtime.query_status(handle.target_ref)
        assert (status.phase, status.pending_code) == ("failed", "provider_terminal_proof_missing")

        operation = owner.latest_operation(handle.target_run_ref)
        assert operation.operation_ref == operation_ref
        assert operation.status == "unknown_outcome"
        assert operation.outcome_code == "provider_terminal_proof_missing"
        assert not runtime.harnesses.external_mcp_scope_is_current(old_token)
        with pytest.raises(AgentRuntimeHarnessError, match="target_root_lifecycle_unavailable"):
            owner.begin_reconciliation(operation_ref)
        with pytest.raises(AgentRuntimeHarnessError, match="target_root_lifecycle_unavailable"):
            owner.start_operation(run_ref=handle.target_run_ref, operation_ref="unsafe-new-operation",
                                  generation=2, invocation_hash="b" * 64, resume=False)
        assert handle.target_ref not in runtime.owners.agent_runtime.list_target_root_work_refs()
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT completed_at FROM ar_harness_provider_operations "
                "WHERE operation_ref=:operation"), {"operation": operation_ref}).scalar_one() is None
            assert connection.execute(text("SELECT count(*) FROM ar_mcp_channel_grants "
                "WHERE run_ref=:run AND status='current'"), {"run": handle.target_run_ref}).scalar_one() == 0
            notice_ref = connection.execute(text("SELECT notice_ref FROM ar_target_work_notices "
                "WHERE target_ref=:target"), {"target": handle.target_ref}).scalar_one()
            assert connection.execute(text("SELECT count(*) FROM ar_bundle_inbox_entries "
                "WHERE notice_ref=:notice"), {"notice": notice_ref}).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM ar_target_root_completions "
                "WHERE target_ref=:target"), {"target": handle.target_ref}).scalar_one() == 0
        notice = runtime.owners.agent_runtime.query_target_work_notice(handle.target_ref)
        assert notice.kind == "coordination_required"
        assert notice.compact_reason == "provider_terminal_proof_missing"
        assert operation_ref in notice.pending_obligation_refs
        handoff = runtime.owners.agent_runtime.read_target_run_handoff(notice.handoff_manifest_ref)
        assert handoff.terminal.reason == "provider_terminal_proof_missing"
        assert handoff.stop_decisions == ()
        assert not handoff.terminal.recovery_ready
        assert runtime.target_root_lifecycle.mark_failed_unknown(target_ref=handle.target_ref) == failed

        # The real Bundle consumer reopens native authority, acknowledges the
        # notice and surfaces its outstanding operation instead of waiting on it.
        with runtime._database.read() as connection:
            stage = connection.execute(text(
                "SELECT s.* FROM ar_stage_runs s JOIN ar_bundle_inbox_entries e "
                "ON e.run_ref=s.run_ref WHERE e.notice_ref=:notice"
            ), {"notice": notice_ref}).one()
            cycle_ref = str(stage.cycle_ref)
            scope = {"run_ref": stage.run_ref, "attempt_ref": stage.current_attempt_ref,
                     "fence_ref": stage.current_fence_ref}
            assert cycle_ref in _cycle_dependencies(connection, (cycle_ref,))
            quest_ref = connection.execute(text(
                "SELECT quest_ref FROM ar_target_launches WHERE target_ref=:target"
            ), {"target": handle.target_ref}).scalar_one()
            author = ResearchRoot("target", handle.target_run_ref, handle.execution_attempt_ref,
                                  handle.root_session_ref, handle.execution_fence_ref,
                                  admission.run.capability_binding_hash, operation_ref)
            with pytest.raises(OwnerConflict, match="goal_author_work_unbound"):
                runtime.owners.research_graph._quest_goal_work_in_transaction(
                    connection, quest_ref=quest_ref, author=author)
        agent_runtime = runtime.owners.agent_runtime
        batch = agent_runtime.read_bundle_inbox(**scope)
        assert batch.notices == (notice,)
        checkpoint = agent_runtime.acknowledge_bundle_inbox(
            **scope, batch=batch, idempotency_key="issue-296-consume-native-interruption")
        assert checkpoint.cursor == batch.next_cursor
        assert agent_runtime.read_bundle_inbox(**scope).notices == ()
        current = runtime.bundle_stage.query_current()
        target = next(item for item in current["target_graph"]["targets"]
                      if item["target_ref"] == handle.target_ref)
        assert target["status"] == "blocked"
        assert target["blocker"]["compact_reason"] == "provider_terminal_proof_missing"
        assert target["blocker"]["pending_obligation_refs"] == [operation_ref]
    finally:
        runtime.close()


def test_unknown_failure_publication_rolls_back_and_can_retry(tmp_path: Path, monkeypatch):
    runtime, handle, _ = _running_root(tmp_path)
    try:
        owner = runtime.owners.agent_runtime.harness_runs
        operation_ref = "issue-296-atomic-unknown"
        owner.start_operation(run_ref=handle.target_run_ref, operation_ref=operation_ref,
                              generation=1, invocation_hash="c" * 64, resume=False)
        owner.record_operation_failure(operation_ref, "provider_terminal_proof_missing",
                                       durable_outcome="unknown",
                                       expected_running_run_ref=handle.target_run_ref)
        runtime.target_root_lifecycle.request_cancel(handle.target_ref, reason="stop unknown")
        feed = runtime.owners.agent_runtime._feed
        record = feed.record

        def crash_before_commit(connection, event, payload):
            if event == "agent_runtime.target_root_failed":
                raise RuntimeError("simulated crash before commit")
            return record(connection, event, payload)

        monkeypatch.setattr(feed, "record", crash_before_commit)
        with pytest.raises(RuntimeError, match="simulated crash"):
            runtime.target_root_lifecycle.mark_failed_unknown(target_ref=handle.target_ref)
        assert runtime.target_root_lifecycle.query(handle.target_ref).status == "running"
        assert runtime.owners.agent_runtime.query_target_work_notice(handle.target_ref) is None
        frontier = runtime.owners.agent_runtime.query_target_frontier_entry(handle.target_ref)
        assert frontier.state == "running"
        monkeypatch.setattr(feed, "record", record)
        failed = runtime.target_root_lifecycle.mark_failed_unknown(target_ref=handle.target_ref)
        assert failed.status == "failed"
        assert runtime.target_root_lifecycle.mark_failed_unknown(target_ref=handle.target_ref) == failed
        with runtime._database.read() as connection:
            assert connection.execute(text(
                "SELECT count(*) FROM ar_target_work_notices WHERE target_ref=:target"
            ), {"target": handle.target_ref}).scalar_one() == 1
        assert owner.latest_operation(handle.target_run_ref).operation_ref == operation_ref
        assert owner.latest_operation(handle.target_run_ref).status == "unknown_outcome"
    finally:
        runtime.close()


def test_unknown_failure_requires_cancel_intent_and_original_operation_diagnostic(tmp_path: Path):
    runtime, handle, _ = _running_root(tmp_path)
    try:
        owner = runtime.owners.agent_runtime.harness_runs
        owner.start_operation(run_ref=handle.target_run_ref, operation_ref="issue-296-no-proof",
                              generation=1, invocation_hash="d" * 64, resume=False)
        with pytest.raises(OwnerConflict, match="target_root_unknown_failure_invalid"):
            runtime.target_root_lifecycle.mark_failed_unknown(target_ref=handle.target_ref)
        runtime.target_root_lifecycle.request_cancel(handle.target_ref)
        with pytest.raises(OwnerConflict, match="target_root_unknown_failure_invalid"):
            runtime.target_root_lifecycle.mark_failed_unknown(target_ref=handle.target_ref)
        assert runtime.target_root_lifecycle.query(handle.target_ref).status == "running"
        assert runtime.owners.agent_runtime.query_target_work_notice(handle.target_ref) is None
    finally:
        runtime.close()


def test_unknown_failure_migration_preserves_existing_root_records(tmp_path: Path, monkeypatch):
    upgrade = command.upgrade
    with monkeypatch.context() as old_schema:
        old_schema.setattr(command, "upgrade", lambda config, _revision:
                           upgrade(config, "0083_reasoning_primary_source"))
        runtime, handle, before = _running_root(tmp_path)
    try:
        database = runtime.data_root.database
        with runtime._database.read() as connection:
            history = connection.execute(text(
                "SELECT * FROM ar_target_root_handle_history WHERE target_ref=:target"
            ), {"target": handle.target_ref}).all()
        upgrade_database(database)
        assert runtime.target_root_lifecycle.query(handle.target_ref) == before
        with runtime._database.read() as connection:
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.execute(text(
                "SELECT * FROM ar_target_root_handle_history WHERE target_ref=:target"
            ), {"target": handle.target_ref}).all() == history
        runtime.target_root_lifecycle.request_cancel(handle.target_ref)
        owner = runtime.owners.agent_runtime.harness_runs
        owner.start_operation(run_ref=handle.target_run_ref, operation_ref="issue-296-upgraded-operation",
                              generation=1, invocation_hash="e" * 64, resume=False)
        owner.record_operation_failure("issue-296-upgraded-operation", "provider_terminal_proof_missing",
                                       durable_outcome="unknown",
                                       expected_running_run_ref=handle.target_run_ref)
        assert runtime.target_root_lifecycle.mark_failed_unknown(target_ref=handle.target_ref).status == "failed"
    finally:
        runtime.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Real Linux late native receipt")
@pytest.mark.parametrize("consumer", ["owner", "daemon", "daemon_effect_retry", "daemon_missing_reconciliation_effect"])
def test_late_signed_receipt_settles_original_operation_without_erasing_failed_handoff(tmp_path: Path, consumer, monkeypatch):
    from meta_research.provider_supervisor import read_transport_envelope, write_transport_envelope
    from meta_research.harness_adapters import CodexHarnessAdapter, HarnessSupervisorTransport

    runtime, handle, _ = _running_root(tmp_path)
    process = None
    try:
        owner = runtime.owners.agent_runtime.harness_runs
        operation_ref = "issue-296-late-original-operation"
        owner_hash = "f" * 64
        owner.start_operation(run_ref=handle.target_run_ref, operation_ref=operation_ref,
                              generation=1, invocation_hash=owner_hash, resume=False)
        transport_root = runtime.harnesses._target_raw_output_store._workspace
        transport = HarnessSupervisorTransport(transport_root)
        key = transport._transport_key
        launches = transport_root / "native-launches"
        completed = transport([
            sys.executable, "-c",
            "import sys; from pathlib import Path; sys.stdin.read(); "
            "Path(sys.argv[1]).write_text('launched\\n'); sys.exit(23)", str(launches),
        ], "isolated late receipt", 4, {
            "META_RESEARCH_HARNESS_FAMILY": "codex",
            "META_RESEARCH_PROVIDER_OPERATION_REF": operation_ref,
            "META_RESEARCH_OWNER_INVOCATION_HASH": owner_hash,
            "META_RESEARCH_HARNESS_WORKSPACE": str(transport_root),
        })
        binding_path = transport_root / "owner-invocation-bindings" / owner_hash[:2] / (owner_hash + ".json")
        binding = read_transport_envelope(binding_path, key)
        digest = binding["transport_invocation_hash"]
        directory = transport_root / "provider-operations" / digest[:2] / digest
        native = read_transport_envelope(directory / "supervisor-exit.json", key)
        binding_path.unlink()  # Withhold only this operation's index until readback.
        # The owner has not received the closure yet. Ending logical work must
        # preserve this exact operation so later evidence can settle it.
        owner.record_operation_failure(operation_ref, "provider_terminal_proof_missing",
                                       durable_outcome="unknown",
                                       expected_running_run_ref=handle.target_run_ref)
        if consumer == "daemon_missing_reconciliation_effect":
            owner.begin_reconciliation(operation_ref)
            owner.record_operation_failure(operation_ref, "provider_terminal_proof_missing",
                durable_outcome="unknown", expected_running_run_ref=handle.target_run_ref)
        runtime.target_root_lifecycle.request_cancel(handle.target_ref, reason="stop unknown")
        failed = runtime.target_root_lifecycle.mark_failed_unknown(target_ref=handle.target_ref)
        agent_runtime = runtime.owners.agent_runtime
        notice = agent_runtime.query_target_work_notice(handle.target_ref)
        original_handoff = agent_runtime.read_target_run_handoff(notice.handoff_manifest_ref)

        assert native["returncode"] == 23
        assert native["process_tree"]["state"] == "closed"
        receipt = {
            "schema_ref": "meta-research/harness-provider-transport-receipt/v1",
            "spool_ref": "provider-spool:" + digest, "transport_invocation_hash": digest,
            "supervisor_receipt_hash": canonical_hash(json.loads((directory / "supervisor-exit.json").read_text())),
            "termination_reason": native["termination_reason"], "provider_returncode": native["returncode"],
        }
        if consumer == "owner":
            write_transport_envelope(binding_path, binding, key)
            reconciled = owner.reconcile_failed_target_operation(operation_ref, receipt)
        else:
            from meta_research.harness import _harness_runtime_effect
            from meta_research.runtime_protection import RuntimeEffectIdentity, RuntimeProtectionUnavailable
            from meta_research.web import ReconciliationHealth, _process_target_runs

            runtime.harnesses._adapters["codex"] = CodexHarnessAdapter(
                transport_root, runner=transport)

            def unavailable_execution_resources(_kind):
                raise OSError("new execution resources are unavailable")

            monkeypatch.setattr("meta_research.target_run_runtime.root_capability_profile",
                                unavailable_execution_resources)
            provider_effect = _harness_runtime_effect(
                run_ref=handle.target_run_ref, attempt_ref=handle.execution_attempt_ref,
                fence_ref=handle.execution_fence_ref, operation_ref=operation_ref,
                target_root=True, reconciling=False)
            # An unrelated external-effect obligation is not discharged by
            # proof that the local provider process tree has ended.
            external_effect = RuntimeEffectIdentity(
                responsibility_ref="issue296-unresolved-external-effect", owner_scope="agent_runtime",
                root_run_ref=handle.target_run_ref, attempt_ref=handle.execution_attempt_ref,
                fence_ref=handle.execution_fence_ref, operation_ref="issue296-external-operation",
                effect_kind="acquisition")
            protection = runtime.harnesses._runtime_protection
            protection.acquire(provider_effect)
            protection.acquire(external_effect)
            finish_calls = []
            finish = runtime.harnesses._finish_runtime_effect

            def finish_with_lost_ack(effect, **kwargs):
                finish_calls.append(effect.responsibility_ref)
                if consumer == "daemon_effect_retry" and len(finish_calls) == 1:
                    raise RuntimeProtectionUnavailable("runtime_test_finish_unavailable")
                return finish(effect, **kwargs)

            monkeypatch.setattr(runtime.harnesses, "_finish_runtime_effect", finish_with_lost_ack)

            async def observe_then_reconcile():
                health = ReconciliationHealth()
                worker = asyncio.create_task(_process_target_runs(runtime, health))
                try:
                    async def wait_until(predicate):
                        async with asyncio.timeout(5):
                            while not predicate():
                                await asyncio.sleep(0.02)

                    await wait_until(lambda: runtime.target_run_runtime.query_status(handle.target_ref) is not None)
                    status = runtime.target_run_runtime.query_status(handle.target_ref)
                    assert (status.phase, status.pending_code) == ("failed", "provider_terminal_proof_missing")
                    assert owner.latest_operation(handle.target_run_ref).status == "unknown_outcome"
                    assert health.status == "ready"
                    # A subsequent existing-daemon sweep finds only this exact
                    # receipt. It must never open another native root turn.
                    write_transport_envelope(binding_path, binding, key)
                    await wait_until(lambda: owner.latest_operation(handle.target_run_ref).outcome_code
                                     == "provider_terminal_reconciled")
                    await wait_until(lambda: not agent_runtime.list_target_root_reconciliation_refs())
                    await wait_until(lambda: health.status == "ready")
                    assert health.status == "ready"
                    assert agent_runtime.list_target_root_reconciliation_refs() == ()
                    with runtime._database.read() as connection:
                        statuses = dict(connection.execute(text(
                            "SELECT responsibility_ref,status FROM ar_execution_responsibilities "
                            "WHERE responsibility_ref IN (:provider,:external)"
                        ), {"provider": provider_effect.responsibility_ref,
                            "external": external_effect.responsibility_ref}).all())
                    assert statuses[provider_effect.responsibility_ref] == "finished"
                    assert statuses[external_effect.responsibility_ref] != "finished"
                    assert len(finish_calls) == (2 if consumer == "daemon_effect_retry" else 1)
                finally:
                    worker.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await worker

            asyncio.run(observe_then_reconcile())
            reconciled = owner.latest_operation(handle.target_run_ref)
        assert (reconciled.operation_ref, reconciled.status, reconciled.outcome_code) == (
            operation_ref, "failed", "provider_terminal_reconciled")
        assert owner.reconcile_failed_target_operation(operation_ref, receipt) == reconciled
        assert owner.query_profile(handle.target_run_ref)["provider_transport_receipts"] == [
            {"provider_operation_ref": operation_ref, **receipt}]
        assert runtime.target_root_lifecycle.query(handle.target_ref) == failed
        assert runtime.target_root_lifecycle.mark_failed_unknown(target_ref=handle.target_ref) == failed
        assert agent_runtime.read_target_run_handoff(notice.handoff_manifest_ref) == original_handoff
        assert agent_runtime.query_target_work_notice(handle.target_ref) == notice
        assert handle.target_ref not in agent_runtime.list_target_root_work_refs()
        assert launches.read_text().splitlines() == ["launched"]
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        runtime.close()
