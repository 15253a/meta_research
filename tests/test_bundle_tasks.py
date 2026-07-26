"""Durable Scheduler/Worker task identity is recovered from provider receipts."""
from __future__ import annotations

import json
import hashlib
from types import SimpleNamespace

import pytest

from orchestrator import database
from orchestrator.bundle_tasks import (
    BundleTaskIdentityError,
    BundleTaskRegistry,
    BundleWorkerTask,
)
from orchestrator.writedaemon import WriteDaemon


def _registry(tmp_path, identities):
    conn = database.connect(tmp_path / "research.sqlite")
    daemon = WriteDaemon(conn)
    with daemon.transaction() as db:
        db.execute(
            "INSERT INTO goal(id,version,text,predicate_json) "
            "VALUES (1,1,'goal','{}')")
        db.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
            "VALUES (1,1,1,'plan','test')")
        db.execute(
            "INSERT INTO build_target(id,cycle_id,target_kind,seq,status) "
            "VALUES (11,1,'build',1,'pending')")
        db.execute(
            "INSERT INTO build_target(id,cycle_id,target_kind,seq,status) "
            "VALUES (12,1,'build',2,'pending')")

    def load_receipt(path, **_expected):
        identity = identities[str(path)]
        if isinstance(identity, SimpleNamespace):
            return identity
        return SimpleNamespace(
            provider_invocation_id=identity,
            execution_outcome="exit",
            execution_returncode=0,
        )

    def load_reviews(sql, *, cycle_id):
        return [
            (int(decision_id), json.loads(raw))
            for decision_id, raw in sql.execute(
                "SELECT id,payload_json FROM decision "
                "WHERE cycle_id=? AND actor='agent' "
                "AND type='runtime_review' ORDER BY id",
                (cycle_id,),
            ).fetchall()
        ]

    return conn, daemon, BundleTaskRegistry(
        daemon, receipt_loader=load_receipt,
        review_loader=load_reviews)


def _account(
        daemon, *, call_id, purpose, provider_ref,
        status="success", accounted_status=None):
    accounted_status = (
        status if accounted_status is None else accounted_status)
    with daemon.transaction() as db:
        db.execute(
            "INSERT INTO runner_call(id,cycle_id,phase,purpose,status) "
            "VALUES (?,1,'bundle',?,?)",
            (call_id, purpose, status),
        )
        db.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (1,'orchestrator','provider_invocation_accounted',?)",
            (json.dumps({
                "protocol": "provider-accounting-v1",
                "runner_call_id": call_id,
                "provider_receipt_ref": provider_ref,
                "execution_receipt_ref": f"exec-{call_id}",
                "runner_terminal_status": accounted_status,
            }, sort_keys=True),),
        )


def _canonical_sha256(payload):
    return "sha256:" + hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _review_child(
        daemon, *, target_id, review_kind, child_thread_id,
        snapshot_ref):
    with daemon.transaction() as db:
        receipt = {
            "protocol": "native-review-receipt-v1",
            "review_request_id": f"nrr-{review_kind}-00000001",
            "cycle_id": "c1",
            "stage": "bundle",
            "target_id": str(target_id),
            "purpose": f"bundle-worker-c1-t{target_id}-n1-a1",
            "review_kind": review_kind,
            "round_no": 1,
            "configured_rounds": 1,
            "runner_call_id": 1,
            "child_thread_id": child_thread_id,
            "verdict": "pass",
            "resulting_subject_hash": (
                "sha256:" + (
                    "a" if review_kind == "bundle_code" else "b") * 64),
        }
        receipt["receipt_hash"] = _canonical_sha256(receipt)
        review_id = db.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (1,'agent','runtime_review',?)",
            (json.dumps(receipt, sort_keys=True),),
        ).lastrowid
        proof = {
            "protocol": "native-review-live-owner-proof-v1",
            "review_decision_id": review_id,
            "review_receipt_hash": receipt["receipt_hash"],
            "cycle_id": "c1",
            "stage": "bundle",
            "target_id": str(target_id),
            "purpose": receipt["purpose"],
            "runner_call_id": 1,
            "child_thread_id": child_thread_id,
            "snapshot_ref": snapshot_ref,
        }
        db.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (1,'orchestrator',"
            "'native_review_live_owner_proof',?)",
            (json.dumps(proof, sort_keys=True),),
        )
        if review_kind == "bundle_code":
            selector = {
                "protocol": "runtime-stage-submission-index-v1",
                "stage": "bundle",
                "target_id": str(target_id),
                "review_decision_id": review_id,
                "artifact_hash": receipt["resulting_subject_hash"],
            }
            db.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (1,'agent','runtime_stage_submission',?)",
                (json.dumps(selector, sort_keys=True),),
            )
        else:
            selector = {
                "protocol": "native-bundle-result-review-ack-v2",
                "cycle_id": "c1",
                "build_target_id": target_id,
                "review_decision_id": review_id,
                "review_receipt_hash": receipt["receipt_hash"],
                "subject_hash": receipt["resulting_subject_hash"],
            }
            db.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (1,'orchestrator',"
                "'runtime_bundle_result_review_ack',?)",
                (json.dumps(selector, sort_keys=True),),
            )
    return review_id


def test_scheduler_and_each_target_recover_one_distinct_task(tmp_path):
    identities = {
        "provider-scheduler": "thread-scheduler",
        "provider-a-1": "thread-a",
        "provider-a-2": "thread-a",
        "provider-b": "thread-b",
    }
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        _account(
            daemon,
            call_id=1,
            purpose="bundle-scheduler-c1-n1-a1",
            provider_ref="provider-scheduler",
        )
        _account(
            daemon,
            call_id=2,
            purpose="bundle-worker-c1-t11-n2-a1",
            provider_ref="provider-a-1",
        )
        _account(
            daemon,
            call_id=3,
            purpose="bundle-worker-c1-t11-n3-a1",
            provider_ref="provider-a-2",
        )
        _account(
            daemon,
            call_id=4,
            purpose="bundle-worker-c1-t12-n4-a1",
            provider_ref="provider-b",
        )

        assert registry.recover("c1", role="scheduler") == "thread-scheduler"
        assert registry.recover(
            "c1", role="target_worker", target_id=11) == "thread-a"
        assert registry.recover(
            "c1", role="target_worker", target_id=12) == "thread-b"
        assert daemon.query_one(
            "SELECT count(*) FROM bundle_worker_task") == (0,)
    finally:
        conn.close()


def test_task_identity_cannot_drift_or_be_shared_across_targets(tmp_path):
    identities = {
        "provider-a": "thread-shared",
        "provider-b": "thread-shared",
        "provider-a-drift": "thread-other",
    }
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        _account(
            daemon,
            call_id=2,
            purpose="bundle-worker-c1-t12-n2-a1",
            provider_ref="provider-b",
        )

        with pytest.raises(
                BundleTaskIdentityError,
                match="多个 Bundle task 共享"):
            registry.recover("c1", role="target_worker", target_id=11)
    finally:
        conn.close()

    drift_root = tmp_path / "drift"
    drift_root.mkdir()
    conn, daemon, registry = _registry(drift_root, identities)
    try:
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        _account(
            daemon,
            call_id=3,
            purpose="bundle-worker-c1-t11-n3-a1",
            provider_ref="provider-a-drift",
        )

        with pytest.raises(
                BundleTaskIdentityError,
                match="provider task identity 漂移"):
            registry.recover("c1", role="target_worker", target_id=11)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("role", "target_id"),
    [
        ("scheduler", 11),
        ("target_worker", None),
        ("other", None),
    ],
)
def test_task_scope_is_closed(role, target_id):
    conn = database.connect(":memory:")
    registry = BundleTaskRegistry(
        WriteDaemon(conn),
        receipt_loader=lambda *_args, **_kwargs: None,
    )
    try:
        with pytest.raises(ValueError):
            registry.recover(
                "c1", role=role, target_id=target_id)
    finally:
        conn.close()


def test_worker_task_lifecycle_freezes_verified_provider_identity(tmp_path):
    identities = {"provider-a": "thread-a"}
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        prepared = registry.prepare_worker("c1", target_id=11)
        assert prepared == BundleWorkerTask(
            id=1,
            cycle_id=1,
            build_target_id=11,
            provider_task_id=None,
            status="created",
            receipt_ref=None,
        )
        assert registry.mark_worker_running(
            "c1", target_id=11).status == "running"

        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='failed' WHERE id=11")
        completed = registry.mark_worker_completed(
            "c1", target_id=11)

        assert completed.provider_task_id == "thread-a"
        assert completed.receipt_ref == "provider-a"
        assert completed.status == "completed"
        assert daemon.query_one(
            "SELECT count(*) FROM bundle_worker_task "
            "WHERE build_target_id=11 AND role='worker'") == (1,)
        assert daemon.query_one(
            "SELECT count(*) FROM bundle_worker_task "
            "WHERE build_target_id=11") == (1,)
    finally:
        conn.close()


def test_terminal_worker_crash_window_is_reconciled_from_receipt(tmp_path):
    identities = {"provider-a": "thread-a"}
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        original = registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='failed' WHERE id=11")

        reconciled = registry.reconcile_terminal_workers("c1")

        assert reconciled == (
            BundleWorkerTask(
                id=original.id,
                cycle_id=1,
                build_target_id=11,
                provider_task_id="thread-a",
                status="completed",
                receipt_ref="provider-a",
            ),
        )
        assert registry.reconcile_terminal_workers("c1") == reconciled
    finally:
        conn.close()


def test_terminal_worker_reconciles_accounted_failed_provider_call(tmp_path):
    identities = {"provider-a": "thread-a"}
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
            status="failed",
        )
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='failed' WHERE id=11")

        assert registry.reconcile_terminal_workers("c1")[0].status == (
            "completed")
    finally:
        conn.close()


def test_terminal_worker_rejects_accounting_status_drift(tmp_path):
    identities = {"provider-a": "thread-a"}
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
            status="failed",
            accounted_status="success",
        )
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='failed' WHERE id=11")

        with pytest.raises(
                BundleTaskIdentityError,
                match="accounting terminal status 漂移"):
            registry.reconcile_terminal_workers("c1")
    finally:
        conn.close()


def test_terminal_worker_recovery_never_synthesizes_missing_worker(tmp_path):
    identities = {"provider-a": "thread-a"}
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='failed' WHERE id=11")

        with pytest.raises(
                BundleTaskIdentityError,
                match="缺既有 Worker task"):
            registry.reconcile_terminal_workers("c1")

        assert daemon.query_one(
            "SELECT count(*) FROM bundle_worker_task") == (0,)
    finally:
        conn.close()


def test_terminal_worker_reconciliation_fails_closed_for_aborted_call(
        tmp_path):
    conn, daemon, registry = _registry(tmp_path, {})
    try:
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        with daemon.transaction() as db:
            db.execute(
                "INSERT INTO runner_call("
                "id,cycle_id,phase,purpose,status) "
                "VALUES (1,1,'bundle',"
                "'bundle-worker-c1-t11-n1-a1','aborted')")
            db.execute(
                "UPDATE build_target SET status='failed' WHERE id=11")

        with pytest.raises(
                BundleTaskIdentityError,
                match="缺可复验 provider identity"):
            registry.reconcile_terminal_workers("c1")

        assert daemon.query_one(
            "SELECT status FROM bundle_worker_task "
            "WHERE build_target_id=11 AND role='worker'") == ("running",)
    finally:
        conn.close()


def test_completed_worker_is_reverified_and_fails_on_unaccounted_call(
        tmp_path):
    identities = {"provider-a": "thread-a"}
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='failed' WHERE id=11")
        registry.mark_worker_completed("c1", target_id=11)
        with daemon.transaction() as db:
            db.execute(
                "INSERT INTO runner_call("
                "id,cycle_id,phase,purpose,status) "
                "VALUES (2,1,'bundle',"
                "'bundle-worker-c1-t11-n2-a1','running')")

        with pytest.raises(
                BundleTaskIdentityError,
                match="provider 调用未收口"):
            registry.reconcile_terminal_workers("c1")
    finally:
        conn.close()


def test_worker_completion_persists_two_authoritative_review_children(
        tmp_path):
    identities = {"provider-a": "thread-worker"}
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        _review_child(
            daemon, target_id=11, review_kind="bundle_code",
            child_thread_id="thread-code-review",
            snapshot_ref="/proof/code-review.json")
        _review_child(
            daemon, target_id=11, review_kind="bundle_result",
            child_thread_id="thread-result-review",
            snapshot_ref="/proof/result-review.json")
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='complete' WHERE id=11")

        completed = registry.mark_worker_completed(
            "c1", target_id=11)

        assert completed.status == "completed"
        assert daemon.query(
            "SELECT role,provider_task_id,status,receipt_ref "
            "FROM bundle_worker_task WHERE build_target_id=11 "
            "ORDER BY role") == [
                (
                    "code_review", "thread-code-review", "completed",
                    "provider-a"),
                (
                    "result_review", "thread-result-review", "completed",
                    "provider-a"),
                (
                    "worker", "thread-worker", "completed", "provider-a"),
            ]
    finally:
        conn.close()


def test_complete_worker_crash_window_recovers_exact_review_ledgers(
        tmp_path):
    identities = {"provider-a": "thread-worker"}
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        _review_child(
            daemon, target_id=11, review_kind="bundle_code",
            child_thread_id="thread-code-review",
            snapshot_ref="/proof/code-review.json")
        _review_child(
            daemon, target_id=11, review_kind="bundle_result",
            child_thread_id="thread-result-review",
            snapshot_ref="/proof/result-review.json")
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='complete' WHERE id=11")

        assert registry.reconcile_terminal_workers("c1")[0].status == (
            "completed")
        assert daemon.query(
            "SELECT role,provider_task_id,status,receipt_ref "
            "FROM bundle_worker_task WHERE build_target_id=11 "
            "ORDER BY role") == [
                (
                    "code_review", "thread-code-review", "completed",
                    "provider-a"),
                (
                    "result_review", "thread-result-review", "completed",
                    "provider-a"),
                (
                    "worker", "thread-worker", "completed", "provider-a"),
            ]
    finally:
        conn.close()


def test_completed_target_reconciliation_rejects_review_status_drift(
        tmp_path):
    identities = {"provider-a": "thread-worker"}
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        _review_child(
            daemon, target_id=11, review_kind="bundle_code",
            child_thread_id="thread-code-review",
            snapshot_ref="/proof/code-review.json")
        _review_child(
            daemon, target_id=11, review_kind="bundle_result",
            child_thread_id="thread-result-review",
            snapshot_ref="/proof/result-review.json")
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='complete' WHERE id=11")
        registry.reconcile_terminal_workers("c1")
        with daemon.transaction() as db:
            db.execute(
                "UPDATE bundle_worker_task SET status='cancelled' "
                "WHERE build_target_id=11 AND role='result_review'")

        with pytest.raises(
                BundleTaskIdentityError,
                match="result_review ledger 非 completed"):
            registry.reconcile_terminal_workers("c1")
    finally:
        conn.close()


def test_complete_worker_reconciliation_fails_without_result_review(
        tmp_path):
    identities = {"provider-a": "thread-worker"}
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        _review_child(
            daemon, target_id=11, review_kind="bundle_code",
            child_thread_id="thread-code-review",
            snapshot_ref="/proof/code-review.json")
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='complete' WHERE id=11")

        with pytest.raises(
                BundleTaskIdentityError,
                match="result_review authority selector"):
            registry.reconcile_terminal_workers("c1")

        assert daemon.query(
            "SELECT role,status FROM bundle_worker_task "
            "WHERE build_target_id=11") == [("worker", "running")]
    finally:
        conn.close()


def test_worker_completion_fails_closed_without_both_review_children(
        tmp_path):
    identities = {"provider-a": "thread-worker"}
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        _review_child(
            daemon, target_id=11, review_kind="bundle_code",
            child_thread_id="thread-code-review",
            snapshot_ref="/proof/code-review.json")
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='complete' WHERE id=11")

        with pytest.raises(
                BundleTaskIdentityError,
                match="result_review authority selector"):
            registry.mark_worker_completed(
                "c1", target_id=11)

        assert daemon.query(
            "SELECT role,status FROM bundle_worker_task "
            "WHERE build_target_id=11") == [("worker", "running")]
    finally:
        conn.close()


def test_interrupted_worker_reuses_same_row_and_provider_task(tmp_path):
    identities = {
        "provider-a-1": "thread-a",
        "provider-a-2": "thread-a",
    }
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        first = registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a-1",
        )

        waiting = registry.mark_worker_interrupted(
            "c1", target_id=11)
        assert waiting.status == "waiting"
        assert waiting.provider_task_id == "thread-a"
        assert waiting.receipt_ref == "provider-a-1"

        resumed = registry.prepare_worker("c1", target_id=11)
        assert resumed.id == first.id
        assert resumed.status == "created"
        assert resumed.provider_task_id == "thread-a"
        assert resumed.receipt_ref == "provider-a-1"
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=2,
            purpose="bundle-worker-c1-t11-n2-a1",
            provider_ref="provider-a-2",
        )
        with daemon.transaction() as db:
            db.execute(
                "UPDATE build_target SET status='failed' WHERE id=11")

        completed = registry.mark_worker_completed(
            "c1", target_id=11)
        assert completed.status == "completed"
        assert completed.provider_task_id == "thread-a"
        assert completed.receipt_ref == "provider-a-1"
        assert daemon.query_one(
            "SELECT count(*) FROM bundle_worker_task "
            "WHERE build_target_id=11 AND role='worker'") == (1,)
    finally:
        conn.close()


def test_unknown_interruption_waits_without_creating_replacement(tmp_path):
    conn, daemon, registry = _registry(tmp_path, {})
    try:
        original = registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)

        waiting = registry.mark_worker_interrupted(
            "c1", target_id=11)

        assert waiting.id == original.id
        assert waiting.status == "waiting"
        assert waiting.provider_task_id is None
        with pytest.raises(
                BundleTaskIdentityError,
                match="缺可续接 provider identity"):
            registry.prepare_worker("c1", target_id=11)
        assert daemon.query_one(
            "SELECT count(*) FROM bundle_worker_task "
            "WHERE build_target_id=11 AND role='worker'") == (1,)
    finally:
        conn.close()


def test_proven_pre_session_exit_can_retry_same_worker_row(tmp_path):
    identities = {
        "provider-pre-session": SimpleNamespace(
            provider_invocation_id=None,
            execution_outcome="exit",
            execution_returncode=2,
        ),
    }
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        original = registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-pre-session",
        )

        failed = registry.mark_worker_interrupted(
            "c1", target_id=11)
        assert failed.status == "failed"
        assert failed.provider_task_id is None

        retry = registry.prepare_worker("c1", target_id=11)
        assert retry.id == original.id
        assert retry.status == "created"
        assert daemon.query_one(
            "SELECT count(*) FROM bundle_worker_task "
            "WHERE build_target_id=11 AND role='worker'") == (1,)
    finally:
        conn.close()


def test_older_safe_exit_cannot_authorize_retry_after_new_unknown_call(
        tmp_path):
    identities = {
        "provider-pre-session": SimpleNamespace(
            provider_invocation_id=None,
            execution_outcome="exit",
            execution_returncode=2,
        ),
    }
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-pre-session",
        )
        registry.mark_worker_interrupted("c1", target_id=11)
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        with daemon.transaction() as db:
            db.execute(
                "INSERT INTO runner_call("
                "id,cycle_id,phase,purpose,status) "
                "VALUES (2,1,'bundle',"
                "'bundle-worker-c1-t11-n2-a1','running')")

        waiting = registry.mark_worker_interrupted(
            "c1", target_id=11)

        assert waiting.status == "waiting"
        with pytest.raises(
                BundleTaskIdentityError,
                match="未收口"):
            registry.prepare_worker("c1", target_id=11)
    finally:
        conn.close()


def test_identity_drift_never_overwrites_proven_worker_binding(tmp_path):
    identities = {
        "provider-a": "thread-a",
        "provider-drift": "thread-drift",
    }
    conn, daemon, registry = _registry(tmp_path, identities)
    try:
        registry.prepare_worker("c1", target_id=11)
        registry.mark_worker_running("c1", target_id=11)
        _account(
            daemon,
            call_id=1,
            purpose="bundle-worker-c1-t11-n1-a1",
            provider_ref="provider-a",
        )
        registry.mark_worker_interrupted("c1", target_id=11)
        _account(
            daemon,
            call_id=2,
            purpose="bundle-worker-c1-t11-n2-a1",
            provider_ref="provider-drift",
        )

        registry.mark_worker_interrupted("c1", target_id=11)

        assert daemon.query_one(
            "SELECT provider_task_id,receipt_ref,status "
            "FROM bundle_worker_task WHERE build_target_id=11 "
            "AND role='worker'") == (
                "thread-a", "provider-a", "waiting")
        with pytest.raises(
                BundleTaskIdentityError,
                match="provider task identity 漂移"):
            registry.prepare_worker("c1", target_id=11)
    finally:
        conn.close()
