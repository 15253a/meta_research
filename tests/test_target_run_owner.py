from __future__ import annotations

import sqlite3
from dataclasses import fields, replace
from pathlib import Path

import pytest
from alembic.operations import Operations
from sqlalchemy import text

from meta_research.bundle_protocol import (
    AcceptedMeasurementClosure,
    BundleInboxBatch,
    TargetLaunchRequest,
    TargetRunHandoff,
    TargetWorkHandle,
    canonical_projection_bytes,
    projection_plain_value,
)
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.migration import upgrade_database
from meta_research.owners.agent_runtime import (
    BUNDLE_DISPATCH_RECEIPT_KIND,
    BUNDLE_INBOX_CHECKPOINT_RECEIPT_KIND,
    BUNDLE_INBOX_CHECKPOINT_SCHEMA,
    BUNDLE_INBOX_OPERATION_CHECKPOINT_RECEIPT_KIND,
    TARGET_RUN_CHECKPOINT_SCHEMA,
    TARGET_LAUNCH_ADMISSION_RECEIPT_KIND,
    SQLiteAgentRuntime,
    _owner_receipt_hash,
    verify_current_target_run_frontier_in_transaction,
)
from meta_research.owners.common import (
    AcceptedTargetCommitTransition,
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
)
from test_plan_stage_migration import _upgrade_to_revision
from test_target_run_worker import (
    _candidate,
    _closure,
    _formal_plan,
    _handle,
    _preflight,
    _receipt,
    _recovered_blocker,
    _snapshot,
    _terminal_blocker,
)


class _UnusedProbe:
    def observe(self):
        raise AssertionError("TargetRun Owner test invoked host probing")


class _CurrentStageVerifier:
    def verify_current_stage_run_request(self, **_values) -> None:
        return None


class _TargetAuthority:
    def __init__(
        self,
        transition: AcceptedTargetCommitTransition | None = None,
    ) -> None:
        self.transition = transition
        self.require_uncommitted_calls: list[bool] = []

    def verify_target_spec_content_receipt(self, **_values) -> None:
        return None

    def verify_target_launch_request(self, _request: object) -> None:
        return None

    def verify_target_candidate_projection_receipt(self, **values) -> None:
        self.require_uncommitted_calls.append(
            values.get("require_uncommitted") is True
        )
        return None

    def query_target_frontier_commit_transition(
        self, target_ref: str
    ) -> AcceptedTargetCommitTransition | None:
        if self.transition is not None and self.transition.target_ref != target_ref:
            raise OwnerConflict("target_commit_transition_invalid")
        return self.transition


class _HarnessAuthority:
    def __init__(self, current: TargetWorkHandle) -> None:
        self.current = current
        self.calls: list[TargetWorkHandle] = []

    def verify_current_target_run_handle(
        self, handle: TargetWorkHandle
    ) -> TargetWorkHandle:
        self.calls.append(handle)
        if handle != self.current:
            raise OwnerConflict("harness_target_run_handle_not_current")
        return self.current

    def verify_current_target_run_scope(self, **_values) -> None:
        return None

    def verify_target_execution_terminal_blocker(
        self,
        *,
        handle: TargetWorkHandle,
        blocker: object,
    ) -> object:
        if handle != self.current:
            raise OwnerConflict("harness_target_run_handle_not_current")
        return blocker


class _HandleOnlyHarnessAuthority:
    def __init__(self, current: TargetWorkHandle) -> None:
        self.current = current

    def verify_current_target_run_handle(
        self, handle: TargetWorkHandle
    ) -> TargetWorkHandle:
        if handle != self.current:
            raise OwnerConflict("harness_target_run_handle_not_current")
        return self.current


def _records():
    candidate = _candidate()
    formal_plan = _formal_plan()
    handle = _handle("owner-a", target_run="target_run_1")
    preflight, scope = _preflight(
        handle,
        formal_plan,
        candidate.implementation_revision_ref,
        "owner-a",
    )
    request = TargetLaunchRequest(
        target_ref=handle.target_ref,
        target_spec_binding=scope.target_spec_binding,
        target_spec_acceptance_receipt=scope.target_spec_acceptance_receipt,
        accepted_input_target_commit_refs=("target-commit-upstream",),
        accepted_input_asset_refs=("asset-1",),
        recoverable_required=True,
    )
    return candidate, formal_plan, handle, preflight, request


def _seed_admitted_launch(path: Path, request: TargetLaunchRequest, handle) -> None:
    launch_ref = "target_launch_1"
    operation_ref = "target_launch_operation_1"
    graph_ref = "target_graph_1"
    stage_request_ref = "stage_request_1"
    quest_ref = "quest_1"
    dispatch_ref = "bundle_dispatch_1"
    dispatch_receipt_ref = "bundle_dispatch_receipt_1"
    run_ref = "bundle_run_1"
    attempt_ref = "bundle_attempt_1"
    fence_ref = "bundle_fence_1"
    native_session_ref = "bundle_native_session_1"
    frontier = [{"target_ref": request.target_ref}]
    state: dict[str, object] = {}
    decision = {
        "schema_ref": "meta-research/bundle-dispatch-decision/v1",
        "run_ref": run_ref,
        "attempt_ref": attempt_ref,
        "fence_ref": fence_ref,
        "native_session_ref": native_session_ref,
        "graph_ref": graph_ref,
        "generation": 1,
        "frontier_hash": canonical_hash(frontier),
        "state_hash": canonical_hash(state),
        "action": "dispatch",
        "selected_target_ref": request.target_ref,
        "rationale": "dispatch exact admitted Target",
    }
    decision_hash = canonical_hash(decision)
    dispatch_receipt_hash = _owner_receipt_hash(
        BUNDLE_DISPATCH_RECEIPT_KIND,
        dispatch_ref,
        {**decision, "decision_hash": decision_hash},
    )
    commit_refs = list(request.accepted_input_target_commit_refs)
    asset_refs = list(request.accepted_input_asset_refs)
    asset_proofs = projection_plain_value(handle.accepted_input_asset_proofs)
    launch_bindings = {
        "operation_ref": operation_ref,
        "target_ref": request.target_ref,
        "graph_ref": graph_ref,
        "stage_request_ref": stage_request_ref,
        "quest_ref": quest_ref,
        "target_spec_content_hash_ref": request.target_spec_binding.content_hash_ref,
        "target_spec_receipt_ref": (
            request.target_spec_acceptance_receipt.receipt_ref
        ),
        "accepted_input_target_commit_refs_hash": canonical_hash(commit_refs),
        "accepted_input_asset_refs_hash": canonical_hash(asset_refs),
        "accepted_input_asset_proofs_hash": canonical_hash(asset_proofs),
        "recoverable_required": True,
        "target_run_ref": handle.target_run_ref,
        "status": "admitted",
        "dispatch_decision_ref": dispatch_ref,
        "dispatch_receipt_ref": dispatch_receipt_ref,
        "dispatch_receipt_hash": dispatch_receipt_hash,
        "human_request_ref": None,
        "human_waiter_ref": None,
        "human_waiter_generation": None,
        "human_authorization_receipt_ref": None,
    }
    launch_command = {
        "command": "admit_target_launch",
        "request": projection_plain_value(request),
        "dispatch_decision_ref": dispatch_ref,
        "human_request_ref": None,
        "human_waiter_ref": None,
        "human_waiter_generation": None,
        "human_authorization_receipt_ref": None,
    }
    launch_receipt_ref = "target_launch_receipt_1"
    launch_receipt_hash = _owner_receipt_hash(
        TARGET_LAUNCH_ADMISSION_RECEIPT_KIND,
        launch_ref,
        launch_bindings,
    )
    empty_batch = BundleInboxBatch(
        after_cursor=0,
        next_cursor=0,
        generation=0,
        notices=(),
    )
    checkpoint_ref = "bundle_inbox_checkpoint_1"
    checkpoint_payload = {
        "schema_ref": BUNDLE_INBOX_CHECKPOINT_SCHEMA,
        "checkpoint_ref": checkpoint_ref,
        "run_ref": run_ref,
        "attempt_ref": attempt_ref,
        "fence_ref": fence_ref,
        "checkpoint_revision": 1,
        "cursor": 0,
        "generation": 0,
        "batch_hash": canonical_hash(projection_plain_value(empty_batch)),
        "closed": True,
    }
    checkpoint_hash = canonical_hash(checkpoint_payload)
    checkpoint_receipt_ref = "bundle_inbox_checkpoint_receipt_1"
    checkpoint_receipt_hash = _owner_receipt_hash(
        BUNDLE_INBOX_CHECKPOINT_RECEIPT_KIND,
        checkpoint_ref,
        {**checkpoint_payload, "checkpoint_hash": checkpoint_hash},
    )
    operation_binding = {
        "schema_ref": "meta-research/bundle-inbox-operation-checkpoint/v1",
        "operation_kind": "dispatch",
        "operation_ref": dispatch_ref,
        "checkpoint_ref": checkpoint_ref,
        "checkpoint_hash": checkpoint_hash,
    }
    operation_binding_hash = canonical_hash(operation_binding)
    operation_receipt_ref = "bundle_inbox_operation_checkpoint_receipt_1"
    operation_receipt_hash = _owner_receipt_hash(
        BUNDLE_INBOX_OPERATION_CHECKPOINT_RECEIPT_KIND,
        dispatch_ref,
        {**operation_binding, "binding_hash": operation_binding_hash},
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO rg_targets (target_ref, graph_ref, target_key, ordinal, "
            "spec_json, spec_hash, dependency_refs_json, dependency_refs_hash, "
            "receipt_ref, receipt_hash, accepted_at, append_ref) VALUES "
            "(?, ?, 'target-key-1', 0, '{}', ?, '[]', ?, "
            "'rg-target-receipt-1', ?, 1.0, NULL)",
            (
                request.target_ref,
                graph_ref,
                request.target_spec_binding.content_hash_ref,
                canonical_hash([]),
                "1" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO ar_stage_runs (run_ref, request_ref, cycle_ref, stage, "
            "epoch, context_pack_ref, context_pack_hash, runtime_binding_json, "
            "runtime_binding_hash, request_receipt_ref, request_receipt_hash, "
            "status, current_attempt_ref, root_session_ref, current_fence_ref, "
            "completion_receipt_ref, completion_receipt_hash, outcome_ref, "
            "admission_key, admission_hash, created_at, updated_at) VALUES "
            "(?, ?, 'cycle-1', 'bundle', 1, 'context-1', ?, '{}', ?, "
            "'stage-request-receipt-1', ?, 'running', ?, 'bundle-root-1', ?, "
            "NULL, NULL, NULL, 'stage-admission-1', ?, 1.0, 1.0)",
            (
                run_ref,
                stage_request_ref,
                "2" * 64,
                canonical_hash({}),
                "3" * 64,
                attempt_ref,
                fence_ref,
                "4" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO ar_stage_sessions (session_ref, run_ref, "
            "native_session_ref, status, created_at, updated_at) VALUES "
            "('bundle-root-1', ?, ?, 'active', 1.0, 1.0)",
            (run_ref, native_session_ref),
        )
        connection.execute(
            "INSERT INTO ar_stage_attempts (attempt_ref, run_ref, generation, "
            "root_session_ref, fence_ref, status, created_at) VALUES "
            "(?, ?, 1, 'bundle-root-1', ?, 'running', 1.0)",
            (attempt_ref, run_ref, fence_ref),
        )
        connection.execute(
            "INSERT INTO ar_execution_fences (fence_ref, run_ref, attempt_ref, "
            "generation, status, issued_at) VALUES (?, ?, ?, 1, 'current', 1.0)",
            (fence_ref, run_ref, attempt_ref),
        )
        connection.execute(
            "INSERT INTO ar_bundle_dispatch_decisions (decision_ref, run_ref, "
            "attempt_ref, fence_ref, native_session_ref, graph_ref, generation, "
            "frontier_json, frontier_hash, state_json, state_hash, action, "
            "selected_target_ref, rationale, decision_hash, idempotency_key, "
            "request_hash, receipt_ref, receipt_hash, created_at) VALUES "
            "(?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'dispatch', ?, ?, ?, "
            "'dispatch-idempotency-1', ?, ?, ?, 1.0)",
            (
                dispatch_ref,
                run_ref,
                attempt_ref,
                fence_ref,
                native_session_ref,
                graph_ref,
                canonical_json(frontier),
                canonical_hash(frontier),
                canonical_json(state),
                canonical_hash(state),
                request.target_ref,
                decision["rationale"],
                decision_hash,
                "5" * 64,
                dispatch_receipt_ref,
                dispatch_receipt_hash,
            ),
        )
        connection.execute(
            "INSERT INTO ar_bundle_inbox_checkpoints (checkpoint_ref, run_ref, "
            "attempt_ref, fence_ref, checkpoint_revision, cursor, generation, "
            "batch_hash, checkpoint_hash, idempotency_key, request_hash, "
            "receipt_ref, receipt_hash, accepted_at) VALUES (?, ?, ?, ?, 1, 0, "
            "0, ?, ?, 'bundle-inbox-seed', ?, ?, ?, 1.0)",
            (
                checkpoint_ref,
                run_ref,
                attempt_ref,
                fence_ref,
                checkpoint_payload["batch_hash"],
                checkpoint_hash,
                "6" * 64,
                checkpoint_receipt_ref,
                checkpoint_receipt_hash,
            ),
        )
        connection.execute(
            "INSERT INTO ar_bundle_inbox_scopes (run_ref, next_sequence, "
            "generation, wake_pending, acknowledged_cursor, "
            "current_checkpoint_ref, updated_at) VALUES (?, 1, 0, 0, 0, ?, 1.0)",
            (run_ref, checkpoint_ref),
        )
        connection.execute(
            "INSERT INTO ar_bundle_inbox_operation_checkpoints "
            "(operation_kind, operation_ref, checkpoint_ref, checkpoint_hash, "
            "binding_hash, receipt_ref, receipt_hash, bound_at) VALUES "
            "('dispatch', ?, ?, ?, ?, ?, ?, 1.0)",
            (
                dispatch_ref,
                checkpoint_ref,
                checkpoint_hash,
                operation_binding_hash,
                operation_receipt_ref,
                operation_receipt_hash,
            ),
        )
        connection.execute(
            "INSERT INTO ar_target_launches (launch_ref, operation_ref, "
            "target_ref, graph_ref, stage_request_ref, quest_ref, "
            "target_spec_content_hash_ref, target_spec_receipt_ref, "
            "target_spec_receipt_subject_ref, "
            "accepted_input_target_commit_refs_json, "
            "accepted_input_target_commit_refs_hash, "
            "accepted_input_asset_refs_json, accepted_input_asset_refs_hash, "
            "accepted_input_asset_proofs_json, "
            "accepted_input_asset_proofs_hash, recoverable_required, "
            "target_run_ref, status, root_session_ref, execution_attempt_ref, "
            "execution_fence_ref, execution_input_binding_ref, "
            "execution_input_binding_receipt_ref, "
            "execution_input_binding_receipt_hash, current_handle_json, "
            "current_handle_hash, dispatch_decision_ref, dispatch_receipt_ref, "
            "dispatch_receipt_hash, human_request_ref, human_waiter_ref, "
            "human_waiter_generation, human_authorization_receipt_ref, "
            "idempotency_key, request_hash, receipt_ref, receipt_hash, "
            "admitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, 1, ?, 'admitted', NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
            "NULL, ?, ?, ?, NULL, NULL, NULL, NULL, 'launch-idempotency-1', ?, "
            "?, ?, 1.0)",
            (
                launch_ref,
                operation_ref,
                request.target_ref,
                graph_ref,
                stage_request_ref,
                quest_ref,
                request.target_spec_binding.content_hash_ref,
                request.target_spec_acceptance_receipt.receipt_ref,
                request.target_spec_acceptance_receipt.subject_ref,
                canonical_json(commit_refs),
                canonical_hash(commit_refs),
                canonical_json(asset_refs),
                canonical_hash(asset_refs),
                canonical_json(asset_proofs),
                canonical_hash(asset_proofs),
                handle.target_run_ref,
                dispatch_ref,
                dispatch_receipt_ref,
                dispatch_receipt_hash,
                canonical_hash(launch_command),
                launch_receipt_ref,
                launch_receipt_hash,
            ),
        )
        connection.commit()


def _runtime(
    path: Path,
    harness: _HarnessAuthority | None,
    target_authority: _TargetAuthority | None = None,
) -> tuple[SQLiteAgentRuntime, Database]:
    database = Database(path)
    runtime = SQLiteAgentRuntime(
        database,
        DurableFeed(database),
        _UnusedProbe(),
        stage_request_verifier=_CurrentStageVerifier(),
        target_graph_verifier=target_authority or _TargetAuthority(),
        target_run_harness_verifier=harness,
        acquisition_private_root=path.parent / "acquisition",
    )
    return runtime, database


def _commit_transition(
    handle: TargetWorkHandle,
    terminal: AcceptedMeasurementClosure,
) -> AcceptedTargetCommitTransition:
    return AcceptedTargetCommitTransition(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        target_commit_ref=terminal.target_commit_ref,
        target_execution_closure_ref="target-execution-closure-1",
        canonical_terminal=terminal,
        issuer_receipt=AcceptanceReceipt(
            issuer="research_graph",
            kind="target_commit_accepted",
            receipt_ref=terminal.rg_target_commit_receipt.receipt_ref,
            subject_ref=terminal.target_commit_ref,
            payload_hash="a" * 64,
        ),
    )


def _lifecycle_counts(database: Database) -> tuple[int, ...]:
    tables = (
        "ar_target_run_activations",
        "ar_target_run_handles",
        "ar_target_run_preflights",
        "ar_target_monitor_states",
        "ar_target_stop_decisions",
        "ar_target_run_recoveries",
        "ar_target_retired_identities",
        "ar_target_handoff_manifests",
        "ar_target_work_notices",
        "ar_target_run_identities",
    )
    with database.read() as connection:
        return tuple(
            int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
            for table in tables
        )


def test_activation_requires_exact_current_harness_identity_before_any_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-run-owner.sqlite3"
    upgrade_database(path)
    candidate, formal_plan, handle, preflight, request = _records()
    _seed_admitted_launch(path, request, handle)
    harness = _HarnessAuthority(handle)
    runtime, database = _runtime(path, harness)
    try:
        before = _lifecycle_counts(database)
        forged = replace(handle, root_session_ref="forged-root-session")
        with pytest.raises(OwnerConflict, match="target_run_harness_identity_invalid"):
            runtime.activate_target_run(
                target_ref=handle.target_ref,
                handle=forged,
                candidate=candidate,
                formal_plan=formal_plan,
                preflight=preflight,
                idempotency_key="forged-handle",
            )
        assert _lifecycle_counts(database) == before
        stale = replace(handle, execution_fence_ref="stale-execution-fence")
        with pytest.raises(OwnerConflict, match="target_run_harness_identity_invalid"):
            runtime.activate_target_run(
                target_ref=handle.target_ref,
                handle=stale,
                candidate=candidate,
                formal_plan=formal_plan,
                preflight=preflight,
                idempotency_key="stale-fence",
            )
        assert _lifecycle_counts(database) == before

        no_authority, no_authority_database = _runtime(path, None)
        try:
            with pytest.raises(
                OwnerConflict, match="target_run_harness_verifier_unavailable"
            ):
                no_authority.activate_target_run(
                    target_ref=handle.target_ref,
                    handle=handle,
                    candidate=candidate,
                    formal_plan=formal_plan,
                    preflight=preflight,
                    idempotency_key="missing-harness-authority",
                )
            assert _lifecycle_counts(no_authority_database) == before
        finally:
            no_authority_database.close()

        handle_only, handle_only_database = _runtime(
            path,
            _HandleOnlyHarnessAuthority(handle),
        )
        try:
            with pytest.raises(
                OwnerConflict, match="target_run_harness_identity_invalid"
            ):
                handle_only.activate_target_run(
                    target_ref=handle.target_ref,
                    handle=handle,
                    candidate=candidate,
                    formal_plan=formal_plan,
                    preflight=preflight,
                    idempotency_key="missing-harness-scope-authority",
                )
            assert _lifecycle_counts(handle_only_database) == before
        finally:
            handle_only_database.close()

        drifted_scope = replace(
            preflight.review_scope,
            semantic_deltas=("semantic-delta-drifted",),
        )
        with pytest.raises(OwnerConflict, match="target_run_preflight_invalid"):
            runtime.activate_target_run(
                target_ref=handle.target_ref,
                handle=handle,
                candidate=candidate,
                formal_plan=formal_plan,
                preflight=replace(preflight, review_scope=drifted_scope),
                idempotency_key="drifted-preflight",
            )
        assert _lifecycle_counts(database) == before

        frontier = runtime.activate_target_run(
            target_ref=handle.target_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            preflight=preflight,
            idempotency_key="activate-target-run",
        )
        assert frontier.state == "running"
        assert frontier.current_handle == handle
        activated = _lifecycle_counts(database)
        assert activated[:4] == (1, 1, 1, 1)
        assert runtime.activate_target_run(
            target_ref=handle.target_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            preflight=preflight,
            idempotency_key="activate-target-run",
        ) == frontier
        assert _lifecycle_counts(database) == activated
    finally:
        database.close()


def test_running_checkpoint_and_frontier_inventory_rebuild_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-run-checkpoint.sqlite3"
    upgrade_database(path)
    candidate, formal_plan, handle, preflight, request = _records()
    _seed_admitted_launch(path, request, handle)
    target_authority = _TargetAuthority()
    runtime, database = _runtime(
        path,
        _HarnessAuthority(handle),
        target_authority,
    )
    frontier = runtime.activate_target_run(
        target_ref=handle.target_ref,
        handle=handle,
        candidate=candidate,
        formal_plan=formal_plan,
        preflight=preflight,
        idempotency_key="activate-checkpoint",
    )
    initial = runtime.query_target_run_checkpoint(handle.target_ref)
    assert initial is not None
    assert tuple(field.name for field in fields(initial)) == (
        "target_ref",
        "frontier",
        "snapshot_required",
        "cursor",
        "status_revision",
        "checkpoint_revision",
        "schema_ref",
    )
    assert initial.schema_ref == TARGET_RUN_CHECKPOINT_SCHEMA
    assert initial.frontier == frontier
    assert initial.snapshot_required is True
    assert initial.cursor is None
    assert initial.status_revision is None
    assert initial.checkpoint_revision == 1
    assert runtime.list_running_target_frontiers() == (frontier,)
    assert target_authority.require_uncommitted_calls
    assert all(target_authority.require_uncommitted_calls)
    with database.write() as connection:
        assert (
            verify_current_target_run_frontier_in_transaction(connection, handle)
            == frontier
        )
        with pytest.raises(OwnerConflict, match="target_run_frontier_not_current"):
            verify_current_target_run_frontier_in_transaction(
                connection,
                replace(handle, execution_fence_ref="forged-fence"),
            )
    database.close()

    restarted, database = _runtime(path, _HarnessAuthority(handle))
    try:
        assert restarted.query_target_run_checkpoint(handle.target_ref) == initial
        assert restarted.list_running_target_frontiers() == (frontier,)

        restarted.record_target_monitor_observation(
            _snapshot(handle),
            idempotency_key="checkpoint-snapshot",
        )
        advanced = restarted.query_target_run_checkpoint(handle.target_ref)
        assert advanced is not None
        assert advanced.snapshot_required is False
        assert advanced.cursor == 1
        assert advanced.status_revision == 1
        assert advanced.checkpoint_revision == 2

        with database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_target_monitor_states SET execution_fence_ref = "
                    "'tampered-fence' WHERE target_ref = :target_ref"
                ),
                {"target_ref": handle.target_ref},
            )
        with pytest.raises(
            OwnerConflict, match="target_run_checkpoint_integrity_invalid"
        ):
            restarted.query_target_run_checkpoint(handle.target_ref)
    finally:
        database.close()


def test_running_frontier_accepts_only_exact_verified_post_commit_transition(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-run-post-commit.sqlite3"
    upgrade_database(path)
    candidate, formal_plan, handle, preflight, request = _records()
    _seed_admitted_launch(path, request, handle)
    target_authority = _TargetAuthority()
    runtime, database = _runtime(
        path,
        _HarnessAuthority(handle),
        target_authority,
    )
    try:
        frontier = runtime.activate_target_run(
            target_ref=handle.target_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            preflight=preflight,
            idempotency_key="activate-post-commit",
        )
        runtime.record_target_monitor_observation(
            _snapshot(handle),
            idempotency_key="snapshot-post-commit",
        )
        terminal = _closure(handle, preflight, candidate)
        target_authority.transition = _commit_transition(handle, terminal)

        assert runtime.query_target_frontier_entry(handle.target_ref) == frontier
        checkpoint = runtime.query_target_run_checkpoint(handle.target_ref)
        assert checkpoint is not None
        assert checkpoint.frontier == frontier
        assert target_authority.require_uncommitted_calls[-1] is False

        target_authority.transition = replace(
            target_authority.transition,
            execution_fence_ref="forged-post-commit-fence",
        )
        with pytest.raises(
            OwnerConflict,
            match="target_commit_transition_invalid",
        ):
            runtime.query_target_run_checkpoint(handle.target_ref)
    finally:
        database.close()


def test_direct_target_run_recovery_is_forbidden_before_any_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-run-direct-recovery.sqlite3"
    upgrade_database(path)
    candidate, formal_plan, handle, preflight, request = _records()
    _seed_admitted_launch(path, request, handle)
    harness = _HarnessAuthority(handle)
    runtime, database = _runtime(path, harness)
    try:
        runtime.activate_target_run(
            target_ref=handle.target_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            preflight=preflight,
            idempotency_key="activate-before-forbidden-recovery",
        )
        blocker = _recovered_blocker(handle)
        replacement_handle = _handle(
            "caller-authored-replacement",
            target_run=handle.target_run_ref,
        )
        before = _lifecycle_counts(database)

        with pytest.raises(
            OwnerConflict,
            match="target_run_direct_recovery_write_forbidden",
        ):
            runtime.recover_target_run(
                target_ref=handle.target_ref,
                blocker=blocker,
                replacement_handle=replacement_handle,
                replacement_preflight=None,
                idempotency_key="forbidden-direct-recovery",
            )

        assert _lifecycle_counts(database) == before
    finally:
        database.close()


@pytest.mark.parametrize(
    ("terminal_kind", "expected_notice_kind"),
    (
        ("measurement", "target_completed"),
        ("blocker", "coordination_required"),
    ),
)
def test_owner_publishes_the_other_fixed_notice_kinds(
    tmp_path: Path,
    terminal_kind: str,
    expected_notice_kind: str,
) -> None:
    path = tmp_path / f"target-run-{terminal_kind}.sqlite3"
    upgrade_database(path)
    candidate, formal_plan, handle, preflight, request = _records()
    _seed_admitted_launch(path, request, handle)
    harness = _HarnessAuthority(handle)
    target_authority = _TargetAuthority()
    runtime, database = _runtime(path, harness, target_authority)
    try:
        runtime.activate_target_run(
            target_ref=handle.target_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            preflight=preflight,
            idempotency_key="activate-terminal-kind",
        )
        runtime.record_target_monitor_observation(
            _snapshot(handle),
            idempotency_key="snapshot-terminal-kind",
        )
        if terminal_kind == "measurement":
            terminal = _closure(handle, preflight, candidate)
            commit_transition = _commit_transition(handle, terminal)
            recovery_evidence: tuple[str, ...] = ()
        else:
            terminal = _terminal_blocker(handle)
            commit_transition = None
            assert terminal.escalation_evidence is not None
            assert terminal.escalation_receipt is not None
            recovery_evidence = tuple(
                sorted(
                    {
                        terminal.blocker_ref,
                        terminal.blocker_receipt.receipt_ref,
                        handle.target_run_ref,
                        handle.root_session_ref,
                        handle.execution_attempt_ref,
                        handle.execution_fence_ref,
                        terminal.escalation_evidence.subject_ref,
                        terminal.escalation_evidence.content_hash_ref,
                        terminal.escalation_receipt.receipt_ref,
                    }
                )
            )
        handoff = TargetRunHandoff(
            handle_history=(handle,),
            code_review_preflights=(preflight,),
            stop_decisions=(),
            recovered_blockers=(),
            recovery_evidence_refs=recovery_evidence,
            terminal=terminal,
        )
        if commit_transition is not None:
            before_unverified_handoff = _lifecycle_counts(database)
            with pytest.raises(
                OwnerConflict,
                match="target_run_handoff_target_commit_invalid",
            ):
                runtime.publish_target_run_handoff(
                    handoff,
                    idempotency_key="reject-unverified-measurement-handoff",
                )
            assert _lifecycle_counts(database) == before_unverified_handoff
            target_authority.transition = replace(
                commit_transition,
                canonical_terminal=replace(terminal, metric_values=(0.5,)),
            )
            with pytest.raises(
                OwnerConflict,
                match="target_run_handoff_target_commit_invalid",
            ):
                runtime.publish_target_run_handoff(
                    handoff,
                    idempotency_key="reject-drifted-measurement-handoff",
                )
            assert _lifecycle_counts(database) == before_unverified_handoff
            target_authority.transition = commit_transition
        notice = runtime.publish_target_run_handoff(
            handoff,
            idempotency_key="publish-terminal-kind",
        )
        assert notice.kind == expected_notice_kind
        assert notice.handoff_manifest_sha256 == canonical_hash(
            projection_plain_value(handoff)
        )
        assert runtime.read_target_run_handoff(notice.handoff_manifest_ref) == handoff
    finally:
        database.close()


def test_target_run_inbox_migration_is_atomic_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "target-run-migration.sqlite3"
    _upgrade_to_revision(path, "0018_target_launch_admission")
    original_create_table = Operations.create_table
    failed_once = False

    def interrupt(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "ar_target_work_notices" and not failed_once:
            failed_once = True
            raise OSError("injected TargetRun Inbox migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", interrupt)
    with pytest.raises(OSError, match="TargetRun Inbox migration interruption"):
        upgrade_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0018_target_launch_admission",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "ar_target_run_activations" not in tables
    assert "ar_target_work_notices" not in tables
    assert "ar_target_frontier_entries" in tables

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(path)
    upgrade_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0034_writing_delivery",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "ar_target_run_activations",
        "ar_target_run_identities",
        "ar_target_monitor_states",
        "ar_target_run_recoveries",
        "ar_target_handoff_manifests",
        "ar_target_work_notices",
        "ar_bundle_inbox_state",
    } <= tables
