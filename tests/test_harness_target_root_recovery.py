from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.bundle_protocol import projection_plain_value
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.harness import HarnessAdmissionError, HarnessRuntime, TargetHarnessRequest
from meta_research.migration import upgrade_database
from meta_research.owners.agent_runtime_harness import (
    TARGET_ROOT_RECOVERY_READY_CODE,
    AgentRuntimeHarnessError,
    AgentRuntimeHarnessRetryLater,
    SQLiteAgentRuntimeHarness,
)
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.semantic_owner_gateway import TARGET_RUN_SEMANTIC_OPERATION_IDS
from meta_research.semantic_mcp import SemanticMcpError
from meta_research.harness_adapters import HARNESS_CAPABILITIES
from test_harness_target_root import _Gateway, _TargetRootAdapter
from test_target_run_owner import _records, _seed_admitted_launch


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture
def immediate_target_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meta_research.owners.agent_runtime_harness."
        "_TARGET_ROOT_RETRY_BASE_SECONDS",
        0.0,
    )


def _owner_with_failed_admission(
    path: Path,
) -> tuple[Database, SQLiteAgentRuntimeHarness, TargetHarnessRequest]:
    upgrade_database(path)
    _candidate, _formal_plan, handle, _preflight, launch_request = _records()
    _seed_admitted_launch(path, launch_request, handle)
    database = Database(path)
    owner = SQLiteAgentRuntimeHarness(database, DurableFeed(database))
    full_conformance = {"contract_ref": "test/full-conformance/v1"}
    request = TargetHarnessRequest(
        request_ref="target-harness-request:failed-recovery",
        harness_family="codex",
        model_ref="gpt-target-root",
        auth_profile_ref="harness-profile:target-root",
        required_operation_ids=TARGET_RUN_SEMANTIC_OPERATION_IDS,
        required_capabilities=HARNESS_CAPABILITIES,
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        full_conformance_binding=full_conformance,
        full_conformance_binding_hash=canonical_hash(full_conformance),
        target_scope_binding_hash="c" * 64,
    )
    reserved = owner.reserve_admission(
        request=request.as_dict(),
        idempotency_key=request.request_ref,
        request_hash=canonical_hash(request.as_dict()),
        capability_binding_hash="b" * 64,
        authoritative_run_ref=handle.target_run_ref,
    )
    owner.fail_admission(reserved.run_ref, "mcp_channel_unavailable")
    return database, owner, request


def _activate_channel(
    owner: SQLiteAgentRuntimeHarness,
    request: TargetHarnessRequest,
):
    run = owner.query_run(request.request_ref)
    assert run is not None
    operation_bindings = [
        {"semantic_operation_id": operation_id}
        for operation_id in request.required_operation_ids
    ]
    mcp_binding = {
        "server_instance_ref": "mcp-server:failed-recovery",
        "endpoint_ref": "/mcp",
        "catalog_revision": 1,
        "catalog_hash": "a" * 64,
        "health_receipt_ref": "mcp-health:failed-recovery",
        "connection_grant_ref": "grant:failed-recovery",
        "operation_bindings": operation_bindings,
    }
    scope = {
        "run_ref": run.run_ref,
        "attempt_ref": run.attempt_ref,
        "root_session_ref": run.root_session_ref,
        "fence_ref": run.fence_ref,
        "capability_binding_hash": run.capability_binding_hash,
        "operation_ids": list(request.required_operation_ids),
    }
    return owner.activate_admission(
        run_ref=run.run_ref,
        mcp_binding=mcp_binding,
        grant_ref="grant:failed-recovery",
        server_instance_ref="mcp-server:failed-recovery",
        token_hash="d" * 64,
        scope=scope,
    )


def _replace_channel(
    owner: SQLiteAgentRuntimeHarness,
    request: TargetHarnessRequest,
    *,
    suffix: str,
):
    run = owner.query_run(request.request_ref)
    assert run is not None
    grant_ref = f"grant:failed-recovery:{suffix}"
    server_ref = f"mcp-server:failed-recovery:{suffix}"
    mcp_binding = {
        "server_instance_ref": server_ref,
        "endpoint_ref": "/mcp",
        "catalog_revision": 1,
        "catalog_hash": "a" * 64,
        "health_receipt_ref": f"mcp-health:failed-recovery:{suffix}",
        "connection_grant_ref": grant_ref,
        "operation_bindings": [
            {"semantic_operation_id": operation_id}
            for operation_id in request.required_operation_ids
        ],
    }
    scope = {
        "run_ref": run.run_ref,
        "attempt_ref": run.attempt_ref,
        "root_session_ref": run.root_session_ref,
        "fence_ref": run.fence_ref,
        "capability_binding_hash": run.capability_binding_hash,
        "operation_ids": list(request.required_operation_ids),
    }
    return owner.replace_channel(
        run_ref=run.run_ref,
        mcp_binding=mcp_binding,
        grant_ref=grant_ref,
        server_instance_ref=server_ref,
        token_hash=canonical_hash({"suffix": suffix}),
        scope=scope,
    )


def _owner_with_active_root(
    path: Path,
) -> tuple[Database, SQLiteAgentRuntimeHarness, TargetHarnessRequest, object]:
    upgrade_database(path)
    candidate, formal_plan, base_handle, _preflight, launch_request = _records()
    _seed_admitted_launch(path, launch_request, base_handle)
    database = Database(path)
    owner = SQLiteAgentRuntimeHarness(database, DurableFeed(database))
    full_conformance = {"contract_ref": "test/full-conformance/v1"}
    request = TargetHarnessRequest(
        request_ref="target-harness-request:active-failed-recovery",
        harness_family="codex",
        model_ref="gpt-target-root",
        auth_profile_ref="harness-profile:target-root",
        required_operation_ids=TARGET_RUN_SEMANTIC_OPERATION_IDS,
        required_capabilities=HARNESS_CAPABILITIES,
        target_ref=base_handle.target_ref,
        target_run_ref=base_handle.target_run_ref,
        full_conformance_binding=full_conformance,
        full_conformance_binding_hash=canonical_hash(full_conformance),
        target_scope_binding_hash="c" * 64,
    )
    reserved = owner.reserve_admission(
        request=request.as_dict(),
        idempotency_key=request.request_ref,
        request_hash=canonical_hash(request.as_dict()),
        capability_binding_hash="b" * 64,
        authoritative_run_ref=base_handle.target_run_ref,
    )
    admitted = _activate_channel(owner, request)
    assert admitted.run_ref == reserved.run_ref
    handle = replace(
        base_handle,
        root_session_ref=admitted.root_session_ref,
        execution_attempt_ref=admitted.attempt_ref,
        execution_fence_ref=admitted.fence_ref,
    )
    handle_value = projection_plain_value(handle)
    candidate_value = projection_plain_value(candidate)
    formal_plan_value = projection_plain_value(formal_plan)
    with database.fenced_write() as connection:
        connection.execute(
            text(
                "INSERT INTO ar_target_root_lifecycles (lifecycle_ref, "
                "target_ref, launch_ref, target_run_ref, root_session_ref, "
                "target_attempt_ref, target_fence_ref, initial_handle_json, "
                "initial_handle_hash, candidate_json, candidate_hash, "
                "formal_plan_json, formal_plan_hash, status, completion_ref, "
                "idempotency_key, request_hash, created_at, updated_at) VALUES "
                "('target-root-lifecycle:failed-recovery', :target_ref, "
                "'target_launch_1', :target_run_ref, :root_session_ref, "
                ":attempt_ref, :fence_ref, :handle_json, :handle_hash, "
                ":candidate_json, :candidate_hash, :formal_plan_json, "
                ":formal_plan_hash, 'running', NULL, "
                "'target-root-lifecycle:failed-recovery', :request_hash, "
                "1.0, 1.0)"
            ),
            {
                "target_ref": handle.target_ref,
                "target_run_ref": handle.target_run_ref,
                "root_session_ref": handle.root_session_ref,
                "attempt_ref": handle.execution_attempt_ref,
                "fence_ref": handle.execution_fence_ref,
                "handle_json": canonical_json(handle_value),
                "handle_hash": canonical_hash(handle_value),
                "candidate_json": canonical_json(candidate_value),
                "candidate_hash": canonical_hash(candidate_value),
                "formal_plan_json": canonical_json(formal_plan_value),
                "formal_plan_hash": canonical_hash(formal_plan_value),
                "request_hash": "e" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO ar_target_frontier_entries (target_ref, "
                "launch_ref, target_spec_content_hash_ref, "
                "target_spec_receipt_ref, target_spec_receipt_subject_ref, "
                "state_revision, state, current_handle_json, "
                "current_handle_hash, terminal_fact_ref, currentness_known, "
                "current, updated_at) VALUES (:target_ref, 'target_launch_1', "
                ":spec_hash, :receipt_ref, :receipt_subject_ref, 1, 'running', "
                ":handle_json, :handle_hash, NULL, 1, 1, 1.0)"
            ),
            {
                "target_ref": handle.target_ref,
                "spec_hash": launch_request.target_spec_binding.content_hash_ref,
                "receipt_ref": (
                    launch_request.target_spec_acceptance_receipt.receipt_ref
                ),
                "receipt_subject_ref": (
                    launch_request.target_spec_acceptance_receipt.subject_ref
                ),
                "handle_json": canonical_json(handle_value),
                "handle_hash": canonical_hash(handle_value),
            },
        )
    return database, owner, request, handle


def test_owner_reopens_failed_target_admission_once_without_rotating_identity(
    tmp_path: Path,
    immediate_target_retry: None,
) -> None:
    database, owner, request = _owner_with_failed_admission(
        tmp_path / "failed-target-admission.sqlite3"
    )
    try:
        failed = owner.query_run(request.request_ref)
        assert failed is not None and failed.status == "failed"
        identity = (
            failed.run_ref,
            failed.root_session_ref,
            failed.attempt_ref,
            failed.fence_ref,
            failed.attempt_generation,
        )

        recovered = owner.reopen_failed_target_root(request.request_ref)
        replayed = owner.reopen_failed_target_root(request.request_ref)

        assert recovered.reopened is True
        assert replayed.reopened is False
        assert recovered.run.status == "admitting"
        assert replayed.run.status == recovered.run.status
        assert replayed.run.failure_code == recovered.run.failure_code
        assert (
            recovered.run.run_ref,
            recovered.run.root_session_ref,
            recovered.run.attempt_ref,
            recovered.run.fence_ref,
            recovered.run.attempt_generation,
        ) == identity
        assert owner.latest_operation(recovered.run.run_ref) is None
        events = DurableFeed(database).read_event_type(
            "agent_runtime.target_root_failure_recovered"
        )
        assert len(events) == 1
    finally:
        database.close()


def test_owner_replays_lost_recovery_ack_after_process_restart(
    tmp_path: Path,
    immediate_target_retry: None,
) -> None:
    path = tmp_path / "failed-target-admission-restart.sqlite3"
    database, owner, request = _owner_with_failed_admission(path)
    recovered = owner.reopen_failed_target_root(request.request_ref)
    assert recovered.reopened is True
    database.close()

    restarted_database = Database(path)
    restarted_owner = SQLiteAgentRuntimeHarness(
        restarted_database,
        DurableFeed(restarted_database),
    )
    try:
        replayed = restarted_owner.reopen_failed_target_root(request.request_ref)

        assert replayed.reopened is False
        assert replayed.run.status == "admitting"
        assert replayed.run.failure_code == "target_root_recovery_pending"
        assert len(
            DurableFeed(restarted_database).read_event_type(
                "agent_runtime.target_root_failure_recovered"
            )
        ) == 1
    finally:
        restarted_database.close()


@pytest.mark.parametrize("prior_success", (False, True))
def test_owner_reopens_only_drained_failed_turn_and_preserves_operation_ledger(
    tmp_path: Path,
    prior_success: bool,
    immediate_target_retry: None,
) -> None:
    database, owner, request, handle = _owner_with_active_root(
        tmp_path / f"failed-target-turn-{prior_success}.sqlite3"
    )
    try:
        run = owner.query_run(request.request_ref)
        assert run is not None
        if prior_success:
            first_ref = f"{run.run_ref}:harness_turn:1"
            owner.start_operation(
                run_ref=run.run_ref,
                operation_ref=first_ref,
                generation=1,
                invocation_hash="1" * 64,
                resume=False,
            )
            owner.complete_operation(
                operation_ref=first_ref,
                run_ref=run.run_ref,
                native_session_ref="native-target-root:stable",
                profile={},
                evidence_events=(),
            )
            run = owner.query_run(request.request_ref)
            assert run is not None and run.status == "executed"
        failed_generation = 2 if prior_success else 1
        failed_ref = f"{run.run_ref}:harness_turn:{failed_generation}"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=failed_ref,
            generation=failed_generation,
            invocation_hash="2" * 64,
            resume=prior_success,
        )
        owner.record_operation_failure(failed_ref, "provider_process_failed")
        identity = (
            run.run_ref,
            run.root_session_ref,
            run.attempt_ref,
            run.fence_ref,
            run.attempt_generation,
        )

        recovered = owner.reopen_failed_target_root(request.request_ref)
        replayed = owner.reopen_failed_target_root(request.request_ref)

        assert recovered.reopened is True
        assert replayed.reopened is False
        assert recovered.run.status == ("executed" if prior_success else "admitted")
        assert recovered.run.native_session_ref == (
            "native-target-root:stable" if prior_success else None
        )
        assert (
            recovered.run.run_ref,
            recovered.run.root_session_ref,
            recovered.run.attempt_ref,
            recovered.run.fence_ref,
            recovered.run.attempt_generation,
        ) == identity
        assert owner.channel_is_current("d" * 64) is False
        assert owner.next_operation_generation(run.run_ref) == failed_generation + 1
        failed = owner.latest_operation(run.run_ref)
        assert failed is not None
        assert (failed.operation_ref, failed.status, failed.outcome_code) == (
            failed_ref,
            "failed",
            "provider_process_failed",
        )
        with pytest.raises(
            AgentRuntimeHarnessError,
            match="harness_operation_state_conflict",
        ):
            owner.complete_operation(
                operation_ref=failed_ref,
                run_ref=run.run_ref,
                native_session_ref="native-target-root:forged-late-completion",
                profile={},
                evidence_events=(),
            )
        events = DurableFeed(database).read_event_type(
            "agent_runtime.target_root_failure_recovered"
        )
        assert len(events) == 1
        assert handle.target_run_ref == recovered.run.run_ref
    finally:
        database.close()


def test_owner_never_reopens_unknown_provider_outcome(tmp_path: Path) -> None:
    database, owner, request, _handle = _owner_with_active_root(
        tmp_path / "unknown-target-turn.sqlite3"
    )
    try:
        run = owner.query_run(request.request_ref)
        assert run is not None
        operation_ref = f"{run.run_ref}:harness_turn:1"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=operation_ref,
            generation=1,
            invocation_hash="3" * 64,
            resume=False,
        )
        owner.record_operation_failure(operation_ref, "provider_timeout")

        with pytest.raises(
            AgentRuntimeHarnessError,
            match="target_root_failure_recovery_unsafe",
        ):
            owner.reopen_failed_target_root(request.request_ref)

        current = owner.query_run(request.request_ref)
        operation = owner.latest_operation(run.run_ref)
        assert current is not None and current.status == "running"
        assert operation is not None and operation.status == "unknown_outcome"
        assert DurableFeed(database).read_event_type(
            "agent_runtime.target_root_failure_recovered"
        ) == ()
    finally:
        database.close()


def test_owner_rate_limits_initial_target_admission_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(90.0)
    monkeypatch.setattr(
        "meta_research.owners.agent_runtime_harness.time.time",
        clock.time,
    )
    database, owner, request = _owner_with_failed_admission(
        tmp_path / "initial-target-admission-backoff.sqlite3"
    )
    try:
        with pytest.raises(AgentRuntimeHarnessRetryLater) as wait:
            owner.reopen_failed_target_root(request.request_ref)
        assert wait.value.retry.operation_generation == 0
        assert wait.value.retry.failure_code == "mcp_channel_unavailable"
        assert wait.value.retry.consecutive_failures == 1
        assert wait.value.retry.next_retry_at == 91.0
        assert owner.latest_operation(request.target_run_ref) is None

        clock.now = 91.0
        recovered = owner.reopen_failed_target_root(request.request_ref)
        assert recovered.reopened is True
        assert recovered.run.status == "admitting"
        assert recovered.next_retry_at == 92.0
    finally:
        database.close()


def test_owner_durably_rate_limits_failed_root_and_pending_channel_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(100.0)
    monkeypatch.setattr(
        "meta_research.owners.agent_runtime_harness.time.time",
        clock.time,
    )
    path = tmp_path / "target-root-durable-backoff.sqlite3"
    database, owner, request, _handle = _owner_with_active_root(path)
    run = owner.query_run(request.request_ref)
    assert run is not None
    operation_ref = f"{run.run_ref}:harness_turn:1"
    owner.start_operation(
        run_ref=run.run_ref,
        operation_ref=operation_ref,
        generation=1,
        invocation_hash="4" * 64,
        resume=False,
    )
    owner.record_operation_failure(operation_ref, "provider_process_failed")
    baseline_feed = DurableFeed(database).current_revision()

    for now in (100.0, 100.2, 100.8):
        clock.now = now
        with pytest.raises(AgentRuntimeHarnessRetryLater) as caught:
            owner.reopen_failed_target_root(request.request_ref)
        assert caught.value.retry.operation_generation == 1
        assert caught.value.retry.failure_code == "provider_process_failed"
        assert caught.value.retry.consecutive_failures == 1
        assert caught.value.retry.next_retry_at == 101.0
        assert DurableFeed(database).current_revision() == baseline_feed
        assert owner.next_operation_generation(run.run_ref) == 2
    database.close()

    restarted_database = Database(path)
    restarted_owner = SQLiteAgentRuntimeHarness(
        restarted_database,
        DurableFeed(restarted_database),
    )
    try:
        with pytest.raises(AgentRuntimeHarnessRetryLater) as restarted_wait:
            restarted_owner.reopen_failed_target_root(request.request_ref)
        assert restarted_wait.value.retry.next_retry_at == 101.0
        assert DurableFeed(restarted_database).current_revision() == baseline_feed

        clock.now = 101.0
        recovered = restarted_owner.reopen_failed_target_root(
            request.request_ref
        )
        assert recovered.reopened is True
        recovery_feed = DurableFeed(restarted_database).current_revision()

        for now in (101.0, 101.2, 101.8):
            clock.now = now
            with pytest.raises(AgentRuntimeHarnessRetryLater) as pending_wait:
                restarted_owner.reopen_failed_target_root(request.request_ref)
            assert pending_wait.value.retry.next_retry_at == 102.0
            assert (
                DurableFeed(restarted_database).current_revision()
                == recovery_feed
            )
            assert restarted_owner.next_operation_generation(run.run_ref) == 2

        restarted_database.close()
        replay_database = Database(path)
        replay_owner = SQLiteAgentRuntimeHarness(
            replay_database,
            DurableFeed(replay_database),
        )
        try:
            with pytest.raises(AgentRuntimeHarnessRetryLater) as replay_wait:
                replay_owner.reopen_failed_target_root(request.request_ref)
            assert replay_wait.value.retry.next_retry_at == 102.0

            clock.now = 102.0
            leased = replay_owner.reopen_failed_target_root(
                request.request_ref
            )
            assert leased.reopened is False
            assert leased.run.updated_at == 102.0
            with pytest.raises(AgentRuntimeHarnessRetryLater) as next_wait:
                replay_owner.reopen_failed_target_root(request.request_ref)
            assert next_wait.value.retry.next_retry_at == 103.0
            assert replay_owner.next_operation_generation(run.run_ref) == 2
        finally:
            replay_database.close()
    finally:
        restarted_database.close()


def test_target_retry_streak_spans_failure_codes_and_success_resets_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(200.0)
    monkeypatch.setattr(
        "meta_research.owners.agent_runtime_harness.time.time",
        clock.time,
    )
    database, owner, request, _handle = _owner_with_active_root(
        tmp_path / "target-root-retry-streak.sqlite3"
    )
    try:
        run = owner.query_run(request.request_ref)
        assert run is not None
        first_ref = f"{run.run_ref}:harness_turn:1"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=first_ref,
            generation=1,
            invocation_hash="5" * 64,
            resume=False,
        )
        first_retry = owner.record_operation_failure(
            first_ref, "provider_process_failed"
        )
        assert first_retry is not None
        assert first_retry.consecutive_failures == 1
        assert first_retry.next_retry_at == 201.0

        clock.now = 201.0
        owner.reopen_failed_target_root(request.request_ref)
        _replace_channel(owner, request, suffix="second")
        second_ref = f"{run.run_ref}:harness_turn:2"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=second_ref,
            generation=2,
            invocation_hash="6" * 64,
            resume=False,
        )
        second_retry = owner.record_operation_failure(
            second_ref, "required_harness_capability_unavailable"
        )
        assert second_retry is not None
        assert second_retry.failure_code == (
            "required_harness_capability_unavailable"
        )
        assert second_retry.consecutive_failures == 2
        assert second_retry.next_retry_at == 203.0

        clock.now = 203.0
        owner.reopen_failed_target_root(request.request_ref)
        _replace_channel(owner, request, suffix="success")
        third_ref = f"{run.run_ref}:harness_turn:3"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=third_ref,
            generation=3,
            invocation_hash="7" * 64,
            resume=False,
        )
        owner.complete_operation(
            operation_ref=third_ref,
            run_ref=run.run_ref,
            native_session_ref="native-target-root:after-retry",
            profile={},
            evidence_events=(),
        )

        clock.now = 204.0
        fourth_ref = f"{run.run_ref}:harness_turn:4"
        owner.start_operation(
            run_ref=run.run_ref,
            operation_ref=fourth_ref,
            generation=4,
            invocation_hash="8" * 64,
            resume=True,
        )
        reset_retry = owner.record_operation_failure(
            fourth_ref, "provider_process_failed"
        )
        assert reset_retry is not None
        assert reset_retry.consecutive_failures == 1
        assert reset_retry.next_retry_at == 205.0
    finally:
        database.close()


def test_harness_runtime_rate_limits_channel_attempts_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(500.0)
    monkeypatch.setattr(
        "meta_research.owners.agent_runtime_harness.time.time",
        clock.time,
    )
    path = tmp_path / "target-root-channel-retry-runtime.sqlite3"
    database, owner, request, _handle = _owner_with_active_root(path)
    run = owner.query_run(request.request_ref)
    assert run is not None
    operation_ref = f"{run.run_ref}:harness_turn:1"
    owner.start_operation(
        run_ref=run.run_ref,
        operation_ref=operation_ref,
        generation=1,
        invocation_hash="9" * 64,
        resume=False,
    )
    owner.record_operation_failure(operation_ref, "provider_process_failed")

    class FlakyGateway(_Gateway):
        attempts = 0
        unavailable = True

        def issue_channel(self, **values: object):
            self.attempts += 1
            if self.unavailable:
                raise SemanticMcpError("mcp_channel_temporarily_unavailable")
            return super().issue_channel(**values)

    gateway = FlakyGateway()
    runtime = HarnessRuntime(
        owner,
        gateway,
        (_TargetRootAdapter("codex"), _TargetRootAdapter("claude")),
    )

    with pytest.raises(HarnessAdmissionError) as initial_wait:
        runtime.recover_failed_target_root(request.request_ref)
    assert initial_wait.value.next_retry_at == 501.0
    assert gateway.attempts == 0

    clock.now = 501.0
    with pytest.raises(
        HarnessAdmissionError,
        match="mcp_channel_temporarily_unavailable",
    ) as channel_failure:
        runtime.recover_failed_target_root(request.request_ref)
    assert channel_failure.value.next_retry_at == 502.0
    assert gateway.attempts == 1
    for now in (501.2, 501.8):
        clock.now = now
        with pytest.raises(HarnessAdmissionError) as pending_wait:
            runtime.recover_failed_target_root(request.request_ref)
        assert pending_wait.value.next_retry_at == 502.0
        assert gateway.attempts == 1
        assert owner.next_operation_generation(run.run_ref) == 2
    database.close()

    restarted_database = Database(path)
    restarted_owner = SQLiteAgentRuntimeHarness(
        restarted_database,
        DurableFeed(restarted_database),
    )
    restarted_gateway = FlakyGateway()
    restarted = HarnessRuntime(
        restarted_owner,
        restarted_gateway,
        (_TargetRootAdapter("codex"), _TargetRootAdapter("claude")),
    )
    try:
        with pytest.raises(HarnessAdmissionError) as restarted_wait:
            restarted.recover_failed_target_root(request.request_ref)
        assert restarted_wait.value.next_retry_at == 502.0
        assert restarted_gateway.attempts == 0

        clock.now = 502.0
        with pytest.raises(HarnessAdmissionError) as second_channel_failure:
            restarted.recover_failed_target_root(request.request_ref)
        assert second_channel_failure.value.next_retry_at == 503.0
        assert restarted_gateway.attempts == 1

        clock.now = 503.0
        restarted_gateway.unavailable = False
        restarted.recover_failed_target_root(request.request_ref)
        recovered = restarted_owner.query_run(request.request_ref)
        assert recovered is not None
        assert recovered.failure_code == TARGET_ROOT_RECOVERY_READY_CODE
        assert restarted_gateway.attempts == 2
        assert restarted_owner.next_operation_generation(run.run_ref) == 2
    finally:
        restarted_database.close()
