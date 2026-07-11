"""Startup reconciliation between durable execution receipts and DB owner intents.

Guardian receipts prove process-tree outcomes only.  They never prove model
envelopes, metrics, checkpoints, reviews, cost receipts, or Gate success.  In
particular, a drained ``exit(0)`` is deliberately left for the owning stage to
recover and validate; this module never synthesizes business success.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, Optional, Tuple

from .process_supervisor import ExecutionRecoveryError, read_receipt


_RUNNER_PROTOCOL = "runner-call-v1"
_OWNER_PROTOCOL = "execution-owner-v1"
_OWNER_KINDS = ("run", "evaluation_attempt", "build_target")
_ACTIVE_RUNNER = ("created", "running")
_ACTIVE_ATTEMPT = ("created", "running")


def _require_terminal(owner: str, receipt: dict) -> None:
    if receipt.get("state") != "terminal" or receipt.get("group_drained") is not True:
        raise ExecutionRecoveryError(f"{owner} receipt 未证明 terminal+drained")


def _runner_failure(receipt: dict) -> Tuple[str, str]:
    outcome = receipt.get("outcome")
    if outcome == "timeout":
        return "failed", "timeout"
    if outcome == "exit":
        return ("failed", "runtime" if receipt.get("returncode") != 0
                else "orphaned_after_exit")
    if outcome == "spawn_failed":
        return "failed", "env_invalid"
    if outcome == "lingering_descendant":
        return "failed", "lingering_descendant"
    if outcome in ("cancelled", "owner_lost"):
        return "aborted", str(outcome)
    if outcome == "owner_lost_before_start":
        return "aborted", "owner_lost_before_start"
    raise ExecutionRecoveryError(f"未知 execution receipt outcome: {outcome!r}")


def _execution_failure(owner_kind: str, receipt: Optional[dict]) -> Tuple[str, str, str]:
    """Return ``(terminal_status, db_failure_kind, exact_failure_kind)``.

    ``run`` has no aborted status and a narrower frozen failure taxonomy, so
    owner-loss is represented as ``failed/aborted`` there.  The exact guardian
    outcome remains losslessly recorded in ``execution_reconciled``.
    """
    if receipt is None:
        exact = "orphaned_without_receipt"
        return (("aborted", "aborted", exact) if owner_kind == "evaluation_attempt"
                else ("failed", "aborted", exact))
    outcome = receipt.get("outcome")
    if outcome == "exit":
        if receipt.get("returncode") == 0:
            raise ExecutionRecoveryError("exit(0) 应走 owner artifact recovery，不得终态化")
        return ("failed", "smoke", "smoke") if owner_kind == "build_target" else (
            "failed", "runtime", "runtime")
    if outcome == "timeout":
        return "failed", "timeout", "timeout"
    if outcome == "spawn_failed":
        # run 的冻结 DDL 不含 env_invalid；精确分类仍落 decision。
        return "failed", ("env_invalid" if owner_kind == "evaluation_attempt" else "runtime"), "env_invalid"
    if outcome == "lingering_descendant":
        return "failed", "runtime", "lingering_descendant"
    if outcome in ("cancelled", "owner_lost", "owner_lost_before_start"):
        return (("aborted", "aborted", str(outcome))
                if owner_kind == "evaluation_attempt"
                else ("failed", "aborted", str(outcome)))
    raise ExecutionRecoveryError(f"未知 execution receipt outcome: {outcome!r}")


class ExecutionReconciler:
    """Reconcile terminal central receipts before any new external call is exposed."""

    def __init__(self, daemon, cost_ledger, receipt_dir: Path):
        self.daemon = daemon
        self.cost_ledger = cost_ledger
        self.receipt_dir = Path(receipt_dir)

    @staticmethod
    def _receipt_hash(path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def _receipts(self) -> Dict[Tuple[str, int], Tuple[Path, dict]]:
        owners: Dict[Tuple[str, int], Tuple[Path, dict]] = {}
        for path in sorted(self.receipt_dir.glob("execution-*.json")):
            receipt = read_receipt(path)
            context = receipt.get("context") or {}
            protocol = context.get("reconcile_protocol")
            if protocol not in (_RUNNER_PROTOCOL, _OWNER_PROTOCOL):
                continue
            owner_kind = context.get("db_owner_kind")
            if protocol == _RUNNER_PROTOCOL and owner_kind != "runner_call":
                raise ExecutionRecoveryError(
                    f"{path.name} 的 {_RUNNER_PROTOCOL} owner kind 非 runner_call")
            if protocol == _OWNER_PROTOCOL and owner_kind not in _OWNER_KINDS:
                raise ExecutionRecoveryError(
                    f"{path.name} 的 {_OWNER_PROTOCOL} owner kind 非 {_OWNER_KINDS}")
            owner_id = context.get("db_owner_id")
            if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id <= 0:
                raise ExecutionRecoveryError(f"{path.name} 缺合法 DB owner id")
            key = (str(owner_kind), owner_id)
            if key in owners:
                raise ExecutionRecoveryError(
                    f"{owner_kind} {owner_id} 对应多个 execution operation")
            owners[key] = (path, receipt)
        return owners

    @staticmethod
    def _runner_context_matches(row, context: dict) -> bool:
        cycle_id, phase, purpose, _status = row
        return (context.get("cycle_id") == (f"c{cycle_id}" if cycle_id is not None else None)
                and context.get("db_phase") == phase
                and context.get("db_purpose") == purpose)

    def _owner_row(self, owner_kind: str, owner_id: int) -> Optional[dict]:
        if owner_kind == "run":
            row = self.daemon.query_one(
                "SELECT cycle_id,build_target_id,kind,status FROM run WHERE id=?", (owner_id,))
            return None if row is None else {
                "cycle_id": row[0], "build_target_id": row[1], "purpose": row[2],
                "status": row[3], "evaluation_id": None,
            }
        if owner_kind == "evaluation_attempt":
            row = self.daemon.query_one(
                "SELECT cycle_id,build_target_id,purpose,status,evaluation_id "
                "FROM evaluation_attempt WHERE id=?", (owner_id,))
            return None if row is None else {
                "cycle_id": row[0], "build_target_id": row[1], "purpose": row[2],
                "status": row[3], "evaluation_id": row[4],
            }
        if owner_kind == "build_target":
            row = self.daemon.query_one(
                "SELECT cycle_id,id,target_kind,status,baseline_id,variant_id "
                "FROM build_target WHERE id=?", (owner_id,))
            return None if row is None else {
                "cycle_id": row[0], "build_target_id": row[1], "purpose": row[2],
                "status": row[3], "evaluation_id": None,
                "baseline_id": row[4], "variant_id": row[5],
            }
        raise ExecutionRecoveryError(f"不支持 execution DB owner: {owner_kind}")

    def _validate_owner_context(self, owner_kind: str, owner_id: int,
                                row: dict, context: dict) -> None:
        expected_phase = {"run": "train", "evaluation_attempt": "eval",
                          "build_target": "smoke"}[owner_kind]
        if (context.get("reconcile_protocol") != _OWNER_PROTOCOL
                or context.get("db_owner_kind") != owner_kind
                or context.get("db_owner_id") != owner_id
                or context.get("cycle_id") != f"c{row['cycle_id']}"
                or context.get("build_target_id") != row["build_target_id"]
                or context.get("phase") != expected_phase):
            raise ExecutionRecoveryError(
                f"{owner_kind} {owner_id} receipt context 与 DB owner 不一致")
        if owner_kind == "run" and context.get("run_id") != owner_id:
            raise ExecutionRecoveryError(f"run {owner_id} receipt 缺 exact run_id")
        if owner_kind == "evaluation_attempt" and context.get("run_id") is not None:
            run_id = context.get("run_id")
            if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
                raise ExecutionRecoveryError(
                    f"evaluation_attempt {owner_id} receipt run_id 非法")
            run = self.daemon.query_one(
                "SELECT build_target_id FROM run WHERE id=?", (run_id,))
            if run is None or run[0] != row["build_target_id"]:
                raise ExecutionRecoveryError(
                    f"evaluation_attempt {owner_id} receipt run_id 与 target 错配")

    @staticmethod
    def _already_decided(conn, owner_kind: str, owner_id: int) -> bool:
        return conn.execute(
            "SELECT 1 FROM decision WHERE actor='orchestrator' AND type='execution_reconciled' "
            "AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.db_owner_kind')=? "
            "AND json_extract(payload_json,'$.db_owner_id')=? LIMIT 1",
            (owner_kind, owner_id)).fetchone() is not None

    def _record_decision(self, conn, *, protocol: str, owner_kind: str, owner_id: int,
                         cycle_id: Optional[int], receipt_path: Optional[Path],
                         receipt: Optional[dict], terminal_status: str,
                         failure_kind: str, db_failure_kind: Optional[str] = None,
                         recovery_action: str = "terminalized") -> None:
        if self._already_decided(conn, owner_kind, owner_id):
            return
        payload = {
            "reconcile_protocol": protocol,
            "db_owner_kind": owner_kind,
            "db_owner_id": owner_id,
            "receipt_ref": str(receipt_path) if receipt_path is not None else None,
            "receipt_sha256": (self._receipt_hash(receipt_path)
                               if receipt_path is not None else None),
            "operation_id": receipt.get("operation_id") if receipt is not None else None,
            "outcome": receipt.get("outcome") if receipt is not None else None,
            "returncode": receipt.get("returncode") if receipt is not None else None,
            "terminal_status": terminal_status,
            "failure_kind": failure_kind,
            "db_failure_kind": db_failure_kind,
            "recovery_action": recovery_action,
            "success_synthesized": False,
        }
        conn.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (?,'orchestrator','execution_reconciled',?)",
            (cycle_id, json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def _reconcile_runner(self, runner_call_id: int, row, receipt_entry) -> bool:
        _cycle_id, _phase, _purpose, status = row
        path, receipt = receipt_entry if receipt_entry is not None else (None, None)
        if receipt is not None:
            _require_terminal(f"runner_call {runner_call_id}", receipt)
            if not self._runner_context_matches(row, receipt.get("context") or {}):
                raise ExecutionRecoveryError(
                    f"runner_call {runner_call_id} receipt context 与 DB owner 不一致")
            terminal_status, failure_kind = _runner_failure(receipt)
        elif status == "created":
            terminal_status, failure_kind = "aborted", "orphaned_unstarted_call"
        else:
            terminal_status, failure_kind = "failed", "orphaned_without_receipt"

        proved_unstarted = (
            status == "created" and receipt is None
        ) or (receipt is not None and receipt.get("outcome") == "owner_lost_before_start")
        with self.daemon.transaction() as conn:
            if self._already_decided(conn, "runner_call", runner_call_id):
                return False
            current = conn.execute(
                "SELECT status FROM runner_call WHERE id=?", (runner_call_id,)).fetchone()
            if current is None:
                raise ExecutionRecoveryError(f"runner_call {runner_call_id} 在对账中消失")
            if current[0] not in _ACTIVE_RUNNER:
                return False
            if proved_unstarted:
                changed = conn.execute(
                    "UPDATE runner_call SET status='aborted',failure_kind=?,"
                    "transcript_ref=COALESCE(?,transcript_ref),finished_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND status IN ('created','running')",
                    (failure_kind, str(path) if path is not None else None,
                     runner_call_id)).rowcount
                if changed != 1:
                    raise ExecutionRecoveryError(
                        f"runner_call {runner_call_id} unstarted 收口竞态")
                terminal_status = "aborted"
            else:
                self.cost_ledger.fail_existing_unaccounted_call(
                    conn, runner_call_id=runner_call_id,
                    failure_kind=failure_kind,
                    terminal_status=terminal_status,
                    cause=RuntimeError(
                        f"startup reconcile: receipt={path} outcome="
                        f"{receipt.get('outcome') if receipt else 'missing'}"))
                if path is not None:
                    conn.execute(
                        "UPDATE runner_call SET transcript_ref=? WHERE id=?",
                        (str(path), runner_call_id))
            self._record_decision(
                conn, protocol=_RUNNER_PROTOCOL, owner_kind="runner_call",
                owner_id=runner_call_id, cycle_id=row[0], receipt_path=path,
                receipt=receipt, terminal_status=terminal_status,
                failure_kind=failure_kind)
        return True

    def _reconcile_owner(self, owner_kind: str, owner_id: int, row: dict,
                         receipt_entry) -> bool:
        path, receipt = receipt_entry if receipt_entry is not None else (None, None)
        if receipt is not None:
            _require_terminal(f"{owner_kind} {owner_id}", receipt)
            self._validate_owner_context(
                owner_kind, owner_id, row, receipt.get("context") or {})

        with self.daemon.transaction() as conn:
            if self._already_decided(conn, owner_kind, owner_id):
                return False
            owner_table = {"run": "run", "evaluation_attempt": "evaluation_attempt",
                           "build_target": "build_target"}[owner_kind]
            current = conn.execute(
                f"SELECT status FROM {owner_table} WHERE id=?", (owner_id,)).fetchone()
            if current is None:
                raise ExecutionRecoveryError(f"{owner_kind} {owner_id} 在对账中消失")
            active_statuses = ({"run": ("running",),
                                "evaluation_attempt": _ACTIVE_ATTEMPT,
                                "build_target": ("building", "smoke", "running")}[owner_kind])
            if current[0] not in active_statuses:
                return False

            if (receipt is not None and receipt.get("outcome") == "exit"
                    and receipt.get("returncode") == 0):
                # The stage must still recover/promote its durable log, parse
                # it, verify artifacts/reviews, and pass Gate.  Keeping the DB
                # intent active preserves that exact owner for deterministic
                # resume without asserting any unproven business outcome.
                if owner_kind == "evaluation_attempt":
                    conn.execute(
                        "UPDATE evaluation_attempt SET transcript_ref=COALESCE(?,transcript_ref) "
                        "WHERE id=? AND status IN ('created','running')",
                        (str(path), owner_id))
                self._record_decision(
                    conn, protocol=_OWNER_PROTOCOL, owner_kind=owner_kind,
                    owner_id=owner_id, cycle_id=row["cycle_id"], receipt_path=path,
                    receipt=receipt, terminal_status=current[0],
                    failure_kind="orphaned_after_exit", db_failure_kind=None,
                    recovery_action="await_owner_artifact_recovery")
                return True

            terminal_status, db_failure, exact_failure = _execution_failure(
                owner_kind, receipt)
            if owner_kind == "run":
                changed = conn.execute(
                    "UPDATE run SET status='failed',failure_kind=? "
                    "WHERE id=? AND status='running'",
                    (db_failure, owner_id)).rowcount
            elif owner_kind == "evaluation_attempt":
                changed = conn.execute(
                    "UPDATE evaluation_attempt SET status=?,failure_kind=?,"
                    "transcript_ref=COALESCE(?,transcript_ref),completed_cycle=? "
                    "WHERE id=? AND status IN ('created','running')",
                    (terminal_status, db_failure, str(path) if path is not None else None,
                     row["cycle_id"], owner_id)).rowcount
                if changed == 1:
                    # A failed/aborted append after an older canonical success
                    # must not regress the evaluation.  Otherwise no active or
                    # successful attempt remains, so expose a retryable failed
                    # evaluation state.
                    conn.execute(
                        "UPDATE evaluation SET status='failed' WHERE id=? AND status<>'success' "
                        "AND NOT EXISTS (SELECT 1 FROM evaluation_attempt "
                        "WHERE evaluation_id=? AND status IN ('created','running','success'))",
                        (row["evaluation_id"], row["evaluation_id"]))
            else:
                changed = conn.execute(
                    "UPDATE build_target SET status='failed',failure_kind=? "
                    "WHERE id=? AND status IN ('building','smoke','running')",
                    (db_failure, owner_id)).rowcount
                if changed == 1 and row["purpose"] in ("build", "exec", "import"):
                    if row["purpose"] in ("build", "import") and row["baseline_id"] is not None:
                        conn.execute(
                            "UPDATE baseline SET status='build_failed' "
                            "WHERE id=? AND status IN ('planned','building')",
                            (row["baseline_id"],))
                    if row["variant_id"] is not None:
                        conn.execute(
                            "UPDATE variant SET status='build_failed' "
                            "WHERE id=? AND status IN ('planned','building')",
                            (row["variant_id"],))
            if changed != 1:
                raise ExecutionRecoveryError(f"{owner_kind} {owner_id} 收口竞态")
            self._record_decision(
                conn, protocol=_OWNER_PROTOCOL, owner_kind=owner_kind,
                owner_id=owner_id, cycle_id=row["cycle_id"], receipt_path=path,
                receipt=receipt, terminal_status=terminal_status,
                failure_kind=exact_failure, db_failure_kind=db_failure)
        return True

    def _validate_terminal_owner(self, owner_kind: str, owner_id: int,
                                 row: dict, receipt: dict) -> None:
        _require_terminal(f"{owner_kind} {owner_id}", receipt)
        self._validate_owner_context(owner_kind, owner_id, row, receipt.get("context") or {})
        if owner_kind == "build_target" and row["status"] == "skipped":
            raise ExecutionRecoveryError(
                f"skipped build_target {owner_id} 不得存在 execution receipt")
        success_status = (row["status"] == "success"
                          or (owner_kind == "build_target" and row["status"] == "complete"))
        if success_status and not (
                receipt.get("outcome") == "exit" and receipt.get("returncode") == 0):
            raise ExecutionRecoveryError(
                f"success {owner_kind} {owner_id} 无 drained exit(0) receipt")

    def reconcile_startup(self) -> int:
        owners = self._receipts()
        reconciled = 0
        seen = set()
        for (owner_kind, owner_id), entry in owners.items():
            if owner_kind == "runner_call":
                row = self.daemon.query_one(
                    "SELECT cycle_id,phase,purpose,status FROM runner_call WHERE id=?", (owner_id,))
                if row is None:
                    raise ExecutionRecoveryError(
                        f"receipt 指向不存在的 runner_call {owner_id}")
                if not self._runner_context_matches(row, entry[1].get("context") or {}):
                    raise ExecutionRecoveryError(
                        f"runner_call {owner_id} receipt context 错配")
                if row[3] in _ACTIVE_RUNNER:
                    reconciled += int(self._reconcile_runner(owner_id, row, entry))
                elif row[3] == "success":
                    receipt = entry[1]
                    _require_terminal(f"runner_call {owner_id}", receipt)
                    if not (receipt.get("outcome") == "exit" and receipt.get("returncode") == 0):
                        raise ExecutionRecoveryError(
                            f"success runner_call {owner_id} 无 drained exit(0) receipt")
            else:
                row = self._owner_row(owner_kind, owner_id)
                if row is None:
                    raise ExecutionRecoveryError(
                        f"receipt 指向不存在的 {owner_kind} {owner_id}")
                self._validate_owner_context(
                    owner_kind, owner_id, row, entry[1].get("context") or {})
                active = ((owner_kind == "run" and row["status"] == "running")
                          or (owner_kind == "evaluation_attempt"
                              and row["status"] in _ACTIVE_ATTEMPT)
                          or (owner_kind == "build_target"
                              and row["status"] in ("building", "smoke", "running")))
                if active:
                    reconciled += int(self._reconcile_owner(owner_kind, owner_id, row, entry))
                else:
                    self._validate_terminal_owner(owner_kind, owner_id, row, entry[1])
            seen.add((owner_kind, owner_id))

        rows = self.daemon.query(
            "SELECT id,cycle_id,phase,purpose,status FROM runner_call "
            "WHERE status IN ('created','running') "
            "AND phase NOT IN ('interaction_query','import_search') ORDER BY id")
        for runner_call_id, cycle_id, phase, purpose, status in rows:
            key = ("runner_call", runner_call_id)
            if key in seen:
                continue
            reconciled += int(self._reconcile_runner(
                runner_call_id, (cycle_id, phase, purpose, status), None))

        for owner_kind, sql in (
                ("run", "SELECT id FROM run WHERE status='running' ORDER BY id"),
                ("evaluation_attempt", "SELECT id FROM evaluation_attempt "
                                       "WHERE status IN ('created','running') ORDER BY id")):
            for (owner_id,) in self.daemon.query(sql):
                key = (owner_kind, owner_id)
                if key in seen:
                    continue
                row = self._owner_row(owner_kind, owner_id)
                if row is None:
                    raise ExecutionRecoveryError(f"{owner_kind} {owner_id} 在扫描中消失")
                reconciled += int(self._reconcile_owner(owner_kind, owner_id, row, None))
        return reconciled
