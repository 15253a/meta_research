from __future__ import annotations

import json
import time

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator.cost_ledger import CostLedger
from orchestrator.execution_reconcile import ExecutionReconciler
from orchestrator.process_supervisor import (ExecutionRecoveryError, ExecutionSupervisor,
                                             atomic_write_receipt)
from orchestrator.writedaemon import WriteDaemon


POLICY = yaml.safe_load(
    (conftest.SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _env(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    daemon = WriteDaemon(db.connect(str(tmp_path / "r.sqlite")))
    conftest.seed_minimal(daemon.conn); daemon.conn.commit()
    receipt_dir = tmp_path / "executions"
    supervisor = ExecutionSupervisor.standalone(receipt_dir)
    return daemon, CostLedger(daemon, POLICY), supervisor


def _receipt(supervisor, *, runner_call_id: int, outcome: str, returncode=None,
             cycle_id="c1", phase="idea", purpose="idea-n1-a1", suffix="0"):
    operation_id = "exec-" + suffix.rjust(32, "0")
    path = supervisor.receipt_dir / f"execution-{operation_id}.json"
    prepared = supervisor._prepared_receipt(
        operation_id=operation_id, kind="codex-runner",
        spec_sha256="sha256:" + "a" * 64, timeout_s=10,
        operation_context={
            "reconcile_protocol": "runner-call-v1",
            "db_owner_kind": "runner_call",
            "db_owner_id": runner_call_id,
            "cycle_id": cycle_id,
            "db_phase": phase,
            "db_purpose": purpose,
        })
    terminal = dict(prepared)
    terminal.update({
        "state": "terminal", "outcome": outcome, "returncode": returncode,
        "started_at_unix": (None if outcome == "owner_lost_before_start" else time.time() - 1),
        "finished_at_unix": time.time(), "group_drained": True,
        "term_sent": outcome not in ("exit", "owner_lost_before_start"),
        "kill_sent": False,
    })
    atomic_write_receipt(path, terminal)
    return path


def _owner_receipt(supervisor, *, owner_kind: str, owner_id: int,
                   build_target_id: int, outcome: str, returncode=None,
                   cycle_id="c1", suffix="10"):
    operation_id = "exec-" + suffix.rjust(32, "0")
    path = supervisor.receipt_dir / f"execution-{operation_id}.json"
    context = {
        "reconcile_protocol": "execution-owner-v1",
        "db_owner_kind": owner_kind,
        "db_owner_id": owner_id,
        "cycle_id": cycle_id,
        "build_target_id": build_target_id,
        "phase": {"run": "train", "evaluation_attempt": "eval",
                  "build_target": "smoke"}[owner_kind],
    }
    if owner_kind == "run":
        context["run_id"] = owner_id
    prepared = supervisor._prepared_receipt(
        operation_id=operation_id, kind="harness",
        spec_sha256="sha256:" + "b" * 64, timeout_s=10,
        operation_context=context)
    terminal = dict(prepared)
    terminal.update({
        "state": "terminal", "outcome": outcome, "returncode": returncode,
        "started_at_unix": (None if outcome == "owner_lost_before_start" else time.time() - 1),
        "finished_at_unix": time.time(), "group_drained": True,
        "term_sent": outcome not in ("exit", "owner_lost_before_start"),
        "kill_sent": False,
    })
    atomic_write_receipt(path, terminal)
    return path


def _active_attempt(daemon):
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO variant(id,baseline_id,variant_key,config_json,status) "
            "VALUES (2,1,'reconcile','{}','building')")
        conn.execute(
            "INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,variant_id,"
            "eval_action,eval_key,evaluation_source) "
            "VALUES (3,1,1,'eval',3,'running',2,'create_evaluation','e2','factory')")
        conn.execute(
            "INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,"
            "created_cycle,build_target_id,target_set_hash) "
            "VALUES (2,2,1,1,'e2','factory','running',1,3,'h2')")
        conn.execute(
            "INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,build_target_id,attempt_no,purpose,status) "
            "VALUES (2,2,1,3,1,'factory','running')")
    return 2, 3


def test_exit_zero_never_synthesizes_runner_success(tmp_path):
    daemon, ledger, supervisor = _env(tmp_path)
    with daemon.transaction() as conn:
        rc = conn.execute(
            "INSERT INTO runner_call(cycle_id,phase,purpose,status,started_at) "
            "VALUES (1,'idea','idea-n1-a1','running',CURRENT_TIMESTAMP)").lastrowid
    _receipt(supervisor, runner_call_id=rc, outcome="exit", returncode=0)
    reconciler = ExecutionReconciler(daemon, ledger, supervisor.receipt_dir)
    assert reconciler.reconcile_startup() == 1
    assert daemon.query_one(
        "SELECT status,failure_kind FROM runner_call WHERE id=?", (rc,)) == (
            "failed", "orphaned_after_exit")
    assert daemon.query_one("SELECT count(*) FROM ledger WHERE runner_call_id=?", (rc,))[0] == 0
    payload = daemon.query_one(
        "SELECT json_extract(payload_json,'$.success_synthesized') FROM decision "
        "WHERE type='execution_reconciled'")[0]
    assert payload == 0
    assert reconciler.reconcile_startup() == 0
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='execution_reconciled'")[0] == 1


def test_owner_lost_before_start_aborts_without_cost_stop(tmp_path):
    daemon, ledger, supervisor = _env(tmp_path)
    with daemon.transaction() as conn:
        rc = conn.execute(
            "INSERT INTO runner_call(cycle_id,phase,purpose,status) "
            "VALUES (1,'idea','idea-n1-a1','created')").lastrowid
    _receipt(supervisor, runner_call_id=rc, outcome="owner_lost_before_start")
    assert ExecutionReconciler(
        daemon, ledger, supervisor.receipt_dir).reconcile_startup() == 1
    assert daemon.query_one(
        "SELECT status,failure_kind FROM runner_call WHERE id=?", (rc,)) == (
            "aborted", "owner_lost_before_start")
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='global_stop'")[0] == 0


def test_receipt_context_mismatch_fails_startup(tmp_path):
    daemon, ledger, supervisor = _env(tmp_path)
    with daemon.transaction() as conn:
        rc = conn.execute(
            "INSERT INTO runner_call(cycle_id,phase,purpose,status) "
            "VALUES (1,'idea','idea-n1-a1','running')").lastrowid
    _receipt(
        supervisor, runner_call_id=rc, outcome="timeout",
        cycle_id="c999", purpose="idea-n1-a1")
    with pytest.raises(ExecutionRecoveryError, match="context"):
        ExecutionReconciler(daemon, ledger, supervisor.receipt_dir).reconcile_startup()


def test_duplicate_receipts_for_one_owner_fail_startup(tmp_path):
    daemon, ledger, supervisor = _env(tmp_path)
    with daemon.transaction() as conn:
        rc = conn.execute(
            "INSERT INTO runner_call(cycle_id,phase,purpose,status) "
            "VALUES (1,'idea','idea-n1-a1','running')").lastrowid
    _receipt(supervisor, runner_call_id=rc, outcome="timeout", suffix="1")
    _receipt(supervisor, runner_call_id=rc, outcome="timeout", suffix="2")
    with pytest.raises(ExecutionRecoveryError, match="多个"):
        ExecutionReconciler(daemon, ledger, supervisor.receipt_dir).reconcile_startup()


def test_exit_zero_attempt_waits_for_owner_artifact_recovery_idempotently(tmp_path):
    daemon, ledger, supervisor = _env(tmp_path)
    aid, bt_id = _active_attempt(daemon)
    receipt = _owner_receipt(
        supervisor, owner_kind="evaluation_attempt", owner_id=aid,
        build_target_id=bt_id, outcome="exit", returncode=0)
    reconciler = ExecutionReconciler(daemon, ledger, supervisor.receipt_dir)
    assert reconciler.reconcile_startup() == 1
    # Process success is not metric/Gate success: preserve the exact running
    # intent for stage-specific log recovery and do not create any result.
    assert daemon.query_one(
        "SELECT status,transcript_ref FROM evaluation_attempt WHERE id=?", (aid,)) == (
            "running", str(receipt))
    assert daemon.query_one("SELECT status FROM evaluation WHERE id=2")[0] == "running"
    assert daemon.query_one(
        "SELECT count(*) FROM metric_result WHERE evaluation_attempt_id=?", (aid,))[0] == 0
    payload = daemon.query_one(
        "SELECT payload_json FROM decision WHERE type='execution_reconciled' "
        "AND json_extract(payload_json,'$.db_owner_kind')='evaluation_attempt'")[0]
    assert json.loads(payload)["recovery_action"] == "await_owner_artifact_recovery"
    assert reconciler.reconcile_startup() == 0


def test_timeout_attempt_is_failed_with_exact_receipt_anchor(tmp_path):
    daemon, ledger, supervisor = _env(tmp_path)
    aid, bt_id = _active_attempt(daemon)
    receipt = _owner_receipt(
        supervisor, owner_kind="evaluation_attempt", owner_id=aid,
        build_target_id=bt_id, outcome="timeout")
    assert ExecutionReconciler(
        daemon, ledger, supervisor.receipt_dir).reconcile_startup() == 1
    assert daemon.query_one(
        "SELECT status,failure_kind,transcript_ref FROM evaluation_attempt WHERE id=?", (aid,)) == (
            "failed", "timeout", str(receipt))
    assert daemon.query_one("SELECT status FROM evaluation WHERE id=2")[0] == "failed"


def test_owner_lost_attempt_aborts_and_can_be_explicitly_retried(tmp_path):
    daemon, ledger, supervisor = _env(tmp_path)
    aid, bt_id = _active_attempt(daemon)
    _owner_receipt(
        supervisor, owner_kind="evaluation_attempt", owner_id=aid,
        build_target_id=bt_id, outcome="owner_lost")
    assert ExecutionReconciler(
        daemon, ledger, supervisor.receipt_dir).reconcile_startup() == 1
    assert daemon.query_one(
        "SELECT status,failure_kind FROM evaluation_attempt WHERE id=?", (aid,)) == (
            "aborted", "aborted")
    payload = daemon.query_one(
        "SELECT json_extract(payload_json,'$.failure_kind') FROM decision "
        "WHERE type='execution_reconciled' "
        "AND json_extract(payload_json,'$.db_owner_kind')='evaluation_attempt'")[0]
    assert payload == "owner_lost"


def test_run_timeout_and_owner_context_mismatch(tmp_path):
    daemon, ledger, supervisor = _env(tmp_path)
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,variant_id) "
            "VALUES (3,1,1,'build',3,'running',1)")
        conn.execute(
            "INSERT INTO run(id,cycle_id,variant_id,build_target_id,kind,status) "
            "VALUES (2,1,1,3,'build','running')")
    _owner_receipt(
        supervisor, owner_kind="run", owner_id=2, build_target_id=3,
        outcome="timeout", cycle_id="c999")
    with pytest.raises(ExecutionRecoveryError, match="context"):
        ExecutionReconciler(daemon, ledger, supervisor.receipt_dir).reconcile_startup()
    assert daemon.query_one("SELECT status FROM run WHERE id=2")[0] == "running"


def _active_smoke_target(daemon):
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO baseline(id,slug,canonical_key,status) "
            "VALUES (2,'smoke','smoke-reconcile','building')")
        conn.execute(
            "INSERT INTO variant(id,baseline_id,variant_key,config_json,status) "
            "VALUES (2,2,'base','{}','building')")
        conn.execute(
            "INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id) "
            "VALUES (3,1,1,'build',3,'building',2,2)")
    return 3


def test_smoke_exit_zero_waits_for_stage_and_nonzero_cascades(tmp_path):
    daemon, ledger, supervisor = _env(tmp_path / "ok")
    bt_id = _active_smoke_target(daemon)
    _owner_receipt(
        supervisor, owner_kind="build_target", owner_id=bt_id,
        build_target_id=bt_id, outcome="exit", returncode=0, suffix="20")
    assert ExecutionReconciler(
        daemon, ledger, supervisor.receipt_dir).reconcile_startup() == 1
    assert daemon.query_one("SELECT status FROM build_target WHERE id=3")[0] == "building"
    assert daemon.query_one(
        "SELECT json_extract(payload_json,'$.success_synthesized') FROM decision "
        "WHERE type='execution_reconciled' AND "
        "json_extract(payload_json,'$.db_owner_kind')='build_target'")[0] == 0

    daemon2, ledger2, supervisor2 = _env(tmp_path / "bad")
    bt_id2 = _active_smoke_target(daemon2)
    _owner_receipt(
        supervisor2, owner_kind="build_target", owner_id=bt_id2,
        build_target_id=bt_id2, outcome="exit", returncode=3, suffix="21")
    assert ExecutionReconciler(
        daemon2, ledger2, supervisor2.receipt_dir).reconcile_startup() == 1
    assert daemon2.query_one(
        "SELECT status,failure_kind FROM build_target WHERE id=3") == ("failed", "smoke")
    assert daemon2.query_one("SELECT status FROM baseline WHERE id=2")[0] == "build_failed"
    assert daemon2.query_one("SELECT status FROM variant WHERE id=2")[0] == "build_failed"


def test_skipped_target_with_execution_receipt_fails_startup(tmp_path):
    daemon, ledger, supervisor = _env(tmp_path)
    bt_id = _active_smoke_target(daemon)
    with daemon.transaction() as conn:
        conn.execute("UPDATE build_target SET status='skipped' WHERE id=?", (bt_id,))
    _owner_receipt(
        supervisor, owner_kind="build_target", owner_id=bt_id,
        build_target_id=bt_id, outcome="exit", returncode=0, suffix="22")

    with pytest.raises(ExecutionRecoveryError, match="skipped"):
        ExecutionReconciler(
            daemon, ledger, supervisor.receipt_dir).reconcile_startup()
