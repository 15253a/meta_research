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

from .durable_stop import active_global_stop
from .process_supervisor import (ExecutionRecoveryError, read_receipt,
                                 validate_execution_receipt)
from .provider_invocation import (ProviderInvocation, load_provider_invocation_receipt,
                                  receipt_runner_call_id,
                                  reconstruct_provider_invocation_receipt,
                                  recovery_terminal)


_RUNNER_PROTOCOL = "runner-call-v1"
_OWNER_PROTOCOL = "execution-owner-v1"
_OWNER_KINDS = ("run", "evaluation_attempt", "build_target")
_ACTIVE_RUNNER = ("created", "running")
_ACTIVE_ATTEMPT = ("created", "running")
_SPECIALIZED_RUNNER_PHASES = ("interaction_query", "import_search")


def _require_terminal(owner: str, receipt: dict) -> None:
    if receipt.get("state") != "terminal" or receipt.get("group_drained") is not True:
        raise ExecutionRecoveryError(f"{owner} receipt 未证明 terminal+drained")


def _receipt_execution_attempt(context: dict) -> int:
    """Normalize pre-attempt smoke receipts to the initial generation."""
    value = context.get("execution_attempt", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionRecoveryError("build_target receipt execution_attempt 非法")
    return value


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
    def _receipt_hash(receipt: dict) -> str:
        # ``read_receipt`` already consumed one no-follow descriptor.  Hash its
        # canonical value instead of reopening the path and recreating the
        # exact hash/open race this reconciliation layer is meant to remove.
        payload = (json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False) + "\n").encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _build_target_execution_attempt(self, owner_id: int) -> int:
        rows = self.daemon.query(
            "SELECT id,payload_json FROM decision WHERE actor='orchestrator' "
            "AND type='bundle_repair_requested' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.build_target_id')=? ORDER BY id",
            (owner_id,))
        next_attempt = 1
        for decision_id, raw in rows:
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as error:
                raise ExecutionRecoveryError(
                    f"bundle repair decision d{decision_id} 损坏") from error
            if (not isinstance(payload, dict)
                    or payload.get("protocol") != "bundle-self-heal-v1"
                    or payload.get("build_target_id") != owner_id
                    or payload.get("round_no") != next_attempt):
                raise ExecutionRecoveryError(
                    f"build_target {owner_id} bundle repair attempt 链损坏")
            next_attempt += 1
        return next_attempt

    def _receipts(self) -> Dict[Tuple[str, int], Tuple[Path, dict]]:
        receipt_sets: Dict[Tuple[str, int], list[Tuple[Path, dict]]] = {}
        for path in sorted(self.receipt_dir.glob("execution-*.json")):
            receipt = read_receipt(path)
            validate_execution_receipt(receipt, path)
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
            receipt_sets.setdefault(key, []).append((path, receipt))

        owners: Dict[Tuple[str, int], Tuple[Path, dict]] = {}
        for (owner_kind, owner_id), entries in receipt_sets.items():
            if owner_kind != "build_target":
                if len(entries) != 1:
                    raise ExecutionRecoveryError(
                        f"{owner_kind} {owner_id} 对应多个 execution operation")
                owners[(owner_kind, owner_id)] = entries[0]
                continue

            row = self._owner_row(owner_kind, owner_id)
            if row is None:
                raise ExecutionRecoveryError(
                    f"receipt 指向不存在的 build_target {owner_id}")
            expected_attempt = self._build_target_execution_attempt(owner_id)
            by_attempt: Dict[int, Tuple[Path, dict]] = {}
            for path, receipt in entries:
                context = receipt.get("context") or {}
                self._validate_owner_context(owner_kind, owner_id, row, context)
                attempt = _receipt_execution_attempt(context)
                if attempt in by_attempt:
                    raise ExecutionRecoveryError(
                        f"build_target {owner_id} execution attempt {attempt} "
                        "对应多个 execution operation")
                if attempt > expected_attempt:
                    raise ExecutionRecoveryError(
                        f"build_target {owner_id} receipt attempt {attempt} "
                        f"超过耐久 repair attempt {expected_attempt}")
                by_attempt[attempt] = (path, receipt)
                if attempt < expected_attempt:
                    _require_terminal(
                        f"build_target {owner_id} superseded attempt {attempt}", receipt)
            current = by_attempt.get(expected_attempt)
            if current is not None:
                owners[(owner_kind, owner_id)] = current
            elif row["status"] in ("complete", "skipped"):
                raise ExecutionRecoveryError(
                    f"{row['status']} build_target {owner_id} 缺当前 execution attempt "
                    f"{expected_attempt} receipt")
        return owners

    def _provider_receipts(self) -> Dict[int, Path]:
        receipts: Dict[int, Path] = {}
        for path in sorted(self.receipt_dir.glob("provider-invocation-rc*.json")):
            runner_call_id = receipt_runner_call_id(path)
            if runner_call_id in receipts:
                raise ExecutionRecoveryError(
                    f"runner_call {runner_call_id} 对应多个 provider receipt")
            receipts[runner_call_id] = path
        return receipts

    @staticmethod
    def _load_provider(path: Path, runner_call_id: int, row,
                       execution_entry) -> ProviderInvocation:
        execution_ref = str(execution_entry[0]) if execution_entry is not None else None
        return load_provider_invocation_receipt(
            path, expected_runner_call_id=runner_call_id,
            expected_cycle_id=(f"c{row[0]}" if row[0] is not None else ""),
            expected_phase=row[1], expected_purpose=row[2],
            expected_execution_receipt_ref=execution_ref)

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
    def _already_decided(conn, owner_kind: str, owner_id: int,
                         execution_attempt: Optional[int] = None) -> bool:
        sql = (
            "SELECT 1 FROM decision WHERE actor='orchestrator' "
            "AND type='execution_reconciled' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.db_owner_kind')=? "
            "AND json_extract(payload_json,'$.db_owner_id')=? ")
        params: tuple = (owner_kind, owner_id)
        if execution_attempt is not None:
            # Reconciliation decisions written before this field are the
            # initial implementation generation, never a wildcard over all
            # future bundle repair attempts.
            sql += "AND COALESCE(json_extract(payload_json,'$.execution_attempt'),1)=? "
            params += (execution_attempt,)
        return conn.execute(sql + "LIMIT 1", params).fetchone() is not None

    def _record_decision(self, conn, *, protocol: str, owner_kind: str, owner_id: int,
                         cycle_id: Optional[int], receipt_path: Optional[Path],
                         receipt: Optional[dict], terminal_status: str,
                         failure_kind: str, db_failure_kind: Optional[str] = None,
                         recovery_action: str = "terminalized",
                         provider_invocation: Optional[ProviderInvocation] = None,
                         execution_attempt: Optional[int] = None) -> None:
        if self._already_decided(
                conn, owner_kind, owner_id, execution_attempt=execution_attempt):
            return
        payload = {
            "reconcile_protocol": protocol,
            "db_owner_kind": owner_kind,
            "db_owner_id": owner_id,
            "receipt_ref": str(receipt_path) if receipt_path is not None else None,
            "receipt_sha256": (self._receipt_hash(receipt)
                               if receipt is not None else None),
            "operation_id": receipt.get("operation_id") if receipt is not None else None,
            "outcome": receipt.get("outcome") if receipt is not None else None,
            "returncode": receipt.get("returncode") if receipt is not None else None,
            "terminal_status": terminal_status,
            "failure_kind": failure_kind,
            "db_failure_kind": db_failure_kind,
            "recovery_action": recovery_action,
            "success_synthesized": False,
            "provider_receipt_ref": (provider_invocation.receipt_ref
                                     if provider_invocation is not None else None),
            "provider_receipt_sha256": (provider_invocation.receipt_sha256
                                        if provider_invocation is not None else None),
            "provider_invocation_id": (provider_invocation.provider_invocation_id
                                       if provider_invocation is not None else None),
        }
        if execution_attempt is not None:
            payload["execution_attempt"] = execution_attempt
        conn.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (?,'orchestrator','execution_reconciled',?)",
            (cycle_id, json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def _reconcile_runner(self, runner_call_id: int, row, receipt_entry,
                          provider_path: Optional[Path] = None) -> bool:
        _cycle_id, _phase, _purpose, status = row
        path, receipt = receipt_entry if receipt_entry is not None else (None, None)
        invocation = (self._load_provider(provider_path, runner_call_id, row, receipt_entry)
                      if provider_path is not None else None)
        if invocation is not None:
            terminal_status, failure_kind = recovery_terminal(invocation)
        elif receipt is not None:
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
        try:
            with self.daemon.transaction() as conn:
                if self._already_decided(conn, "runner_call", runner_call_id):
                    return False
                current = conn.execute(
                    "SELECT status FROM runner_call WHERE id=?", (runner_call_id,)).fetchone()
                if current is None:
                    raise ExecutionRecoveryError(f"runner_call {runner_call_id} 在对账中消失")
                if current[0] not in _ACTIVE_RUNNER:
                    return False
                if invocation is not None:
                    self.cost_ledger.finish_call_in_txn(
                        conn, runner_call_id=runner_call_id,
                        status=terminal_status, failure_kind=failure_kind,
                        usage=invocation.usage, transcript_ref=str(path),
                        execution_receipt_ref=invocation.execution_receipt_ref,
                        provider_receipt_ref=invocation.receipt_ref)
                elif proved_unstarted:
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
                    failure_kind=failure_kind,
                    recovery_action=("accounted_provider_invocation"
                                     if invocation is not None else "terminalized"),
                    provider_invocation=invocation)
            return True
        except ValueError as error:
            if invocation is None:
                raise
            # The receipt honestly says usage is unavailable/conflicting (or
            # cannot be represented under the live billing policy).  Preserve
            # that fact and take the existing durable fail-closed path.
            with self.daemon.transaction() as conn:
                current = conn.execute(
                    "SELECT status FROM runner_call WHERE id=?", (runner_call_id,)).fetchone()
                if current is None or current[0] not in _ACTIVE_RUNNER:
                    raise ExecutionRecoveryError(
                        f"runner_call {runner_call_id} provider usage 失败后状态漂移")
                self.cost_ledger.fail_existing_unaccounted_call(
                    conn, runner_call_id=runner_call_id,
                    failure_kind="cost_accounting", cause=error,
                    terminal_status="failed")
                self._record_decision(
                    conn, protocol=_RUNNER_PROTOCOL, owner_kind="runner_call",
                    owner_id=runner_call_id, cycle_id=row[0], receipt_path=path,
                    receipt=receipt, terminal_status="failed",
                    failure_kind="cost_accounting",
                    recovery_action="provider_usage_unaccounted",
                    provider_invocation=invocation)
            return True

    def _reconcile_owner(self, owner_kind: str, owner_id: int, row: dict,
                         receipt_entry) -> bool:
        path, receipt = receipt_entry if receipt_entry is not None else (None, None)
        execution_attempt = (
            _receipt_execution_attempt(receipt.get("context") or {})
            if owner_kind == "build_target" and receipt is not None else
            self._build_target_execution_attempt(owner_id)
            if owner_kind == "build_target" else None)
        if receipt is not None:
            _require_terminal(f"{owner_kind} {owner_id}", receipt)
            self._validate_owner_context(
                owner_kind, owner_id, row, receipt.get("context") or {})

        with self.daemon.transaction() as conn:
            if self._already_decided(
                    conn, owner_kind, owner_id,
                    execution_attempt=execution_attempt):
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
                    recovery_action="await_owner_artifact_recovery",
                    execution_attempt=execution_attempt)
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
                failure_kind=exact_failure, db_failure_kind=db_failure,
                execution_attempt=execution_attempt)
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

    def _validate_terminal_provider(self, invocation: ProviderInvocation) -> None:
        """A terminal call with a provider receipt is either accounted once or visibly fail-closed."""
        row = self.daemon.query_one(
            "SELECT status,failure_kind FROM runner_call WHERE id=?",
            (invocation.runner_call_id,))
        if row is None or row[0] in _ACTIVE_RUNNER:
            raise ExecutionRecoveryError(
                f"runner_call {invocation.runner_call_id} 尚未终态，不能验证 provider accounting")
        ledger_rows = self.daemon.query(
            "SELECT tokens_input,tokens_output,tokens_total,wallclock_sec FROM ledger "
            "WHERE runner_call_id=?", (invocation.runner_call_id,))
        decisions = self.daemon.query(
            "SELECT payload_json FROM decision WHERE actor='orchestrator' "
            "AND type='provider_invocation_accounted' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.runner_call_id')=? ORDER BY id",
            (invocation.runner_call_id,))
        if len(ledger_rows) == 1 and len(decisions) == 1:
            payload = json.loads(decisions[0][0])
            usage = invocation.usage
            if (payload.get("provider_receipt_ref") != invocation.receipt_ref
                    or payload.get("provider_receipt_sha256") != invocation.receipt_sha256
                    or payload.get("local_invocation_id") != invocation.local_invocation_id
                    or tuple(ledger_rows[0]) != (
                        usage.tokens_input, usage.tokens_output, usage.tokens_total,
                        usage.wallclock_sec)):
                raise ExecutionRecoveryError(
                    f"runner_call {invocation.runner_call_id} provider accounting 锚不一致")
            return
        # A failed call with unavailable token usage may be explicitly waived
        # by the local operator.  This is an append-only recovery, not a fake
        # provider accounting receipt: the exact stop, release, provider
        # receipt and zero-charge ledger disposition must all be cross-linked.
        # Ordinary calls still require the exactly-once branch above.
        waivers = self.daemon.query(
            "SELECT id,payload_json FROM decision WHERE actor='orchestrator' "
            "AND type='cost_accounting_waiver_correction' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.runner_call_id')=? ORDER BY id",
            (invocation.runner_call_id,))
        if len(decisions) == 0 and len(waivers) == 1 and len(ledger_rows) == 1:
            waiver = json.loads(waivers[0][1])
            ledger = self.daemon.query_one(
                "SELECT id,tokens_input,tokens_output,tokens_total,wallclock_sec,money "
                "FROM ledger WHERE runner_call_id=?",
                (invocation.runner_call_id,))
            stop = self.daemon.query_one(
                "SELECT payload_json FROM decision WHERE id=? AND actor='orchestrator' "
                "AND type='global_stop'",
                (waiver.get("global_stop_id"),))
            release = self.daemon.query_one(
                "SELECT payload_json FROM decision WHERE id=? AND actor='orchestrator' "
                "AND type='global_stop_release'",
                (waiver.get("release_decision_id"),))
            stop_payload = json.loads(stop[0]) if stop is not None else None
            release_payload = json.loads(release[0]) if release is not None else None
            valid = (
                waiver.get("protocol") == "cost-accounting-waiver-v2"
                and row[1] == "cost_accounting"
                and invocation.usage.tokens_known is False
                and invocation.execution_outcome != "exit"
                and waiver.get("provider_receipt_ref") == invocation.receipt_ref
                and waiver.get("provider_receipt_sha256") == invocation.receipt_sha256
                and waiver.get("execution_receipt_ref") == invocation.execution_receipt_ref
                and waiver.get("execution_receipt_sha256") == invocation.execution_receipt_sha256
                and waiver.get("execution_outcome") == invocation.execution_outcome
                and waiver.get("tokens_known") is False
                and ledger is not None
                and waiver.get("ledger_id") == ledger[0]
                and tuple(ledger[1:4]) == (0, 0, 0)
                and float(ledger[5]) == 0.0
                and isinstance(stop_payload, dict)
                and stop_payload.get("reason") == "cost_accounting_failed"
                and stop_payload.get("runner_call_id") == invocation.runner_call_id
                and isinstance(release_payload, dict)
                and release_payload.get("global_stop_id") == waiver.get("global_stop_id")
                and release_payload.get("runner_call_id") == invocation.runner_call_id
            )
            if valid:
                return
            raise ExecutionRecoveryError(
                f"runner_call {invocation.runner_call_id} cost accounting waiver 锚不一致")
        if waivers:
            raise ExecutionRecoveryError(
                f"runner_call {invocation.runner_call_id} cost accounting waiver 非 exactly-once")
        if ledger_rows or decisions:
            raise ExecutionRecoveryError(
                f"runner_call {invocation.runner_call_id} provider accounting 非 exactly-once")
        stopped = self.daemon.query_one(
            "SELECT 1 FROM decision WHERE actor='orchestrator' AND type='global_stop' "
            "AND json_extract(payload_json,'$.reason')='cost_accounting_failed' LIMIT 1")
        if row[1] not in ("cost_accounting", "orphaned_query_intent") or stopped is None:
            raise ExecutionRecoveryError(
                f"runner_call {invocation.runner_call_id} 有 provider receipt 却既未入账也未 fail-closed")

    def waive_unavailable_provider_usage(self, runner_call_id: int) -> Dict[str, int]:
        """Append an operator-authorized zero-charge disposition and release.

        This is deliberately narrower than a generic stop override.  It accepts
        only an already reconciled failed runner whose immutable provider
        receipt says token usage is unknown and whose guardian outcome is not
        ``exit``.  The original failure/global stop remain append-only facts;
        one zero-token ledger row, one exact stop release and one correction
        record are committed together.  Startup validation already understands
        and re-verifies this v2 correction on every later owner generation.
        """
        if (isinstance(runner_call_id, bool) or not isinstance(runner_call_id, int)
                or runner_call_id <= 0):
            raise ValueError("runner_call_id 须为正整数")
        row = self.daemon.query_one(
            "SELECT cycle_id,phase,purpose,status,failure_kind FROM runner_call WHERE id=?",
            (runner_call_id,))
        if row is None:
            raise ExecutionRecoveryError(
                f"runner_call {runner_call_id} 不存在，不能执行成本 waiver")
        cycle_id, phase, purpose, status, failure_kind = row
        provider_path = self.receipt_dir / f"provider-invocation-rc{runner_call_id}.json"
        invocation = load_provider_invocation_receipt(
            provider_path, expected_runner_call_id=runner_call_id,
            expected_cycle_id=f"c{cycle_id}", expected_phase=phase,
            expected_purpose=purpose)
        if invocation.usage.tokens_known is not False:
            raise ExecutionRecoveryError(
                f"runner_call {runner_call_id} token usage 已知，不得 waiver")
        if invocation.execution_outcome == "exit":
            raise ExecutionRecoveryError(
                f"runner_call {runner_call_id} execution=exit，不得按中断 waiver")

        existing = self.daemon.query(
            "SELECT id,payload_json FROM decision WHERE actor='orchestrator' "
            "AND type='cost_accounting_waiver_correction' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.runner_call_id')=? ORDER BY id",
            (runner_call_id,))
        if existing:
            if len(existing) != 1:
                raise ExecutionRecoveryError(
                    f"runner_call {runner_call_id} 已有多个成本 waiver")
            self._validate_terminal_provider(invocation)
            payload = json.loads(existing[0][1])
            return {
                "waiver_decision_id": int(existing[0][0]),
                "release_decision_id": int(payload["release_decision_id"]),
                "ledger_id": int(payload["ledger_id"]),
            }

        if status != "failed" or failure_kind != "cost_accounting":
            raise ExecutionRecoveryError(
                f"runner_call {runner_call_id} 不是 failed/cost_accounting")
        with self.daemon.transaction() as conn:
            current = conn.execute(
                "SELECT cycle_id,phase,purpose,status,failure_kind FROM runner_call WHERE id=?",
                (runner_call_id,)).fetchone()
            if current != row:
                raise ExecutionRecoveryError(
                    f"runner_call {runner_call_id} waiver 前状态漂移")
            stop = active_global_stop(conn)
            if stop is None:
                raise ExecutionRecoveryError("没有可释放的 active global_stop")
            stop_id, stop_payload = stop
            if (stop_payload.get("reason") != "cost_accounting_failed"
                    or stop_payload.get("runner_call_id") != runner_call_id):
                raise ExecutionRecoveryError(
                    "active global_stop 不属于本次未知成本调用")
            if conn.execute(
                    "SELECT 1 FROM ledger WHERE runner_call_id=? LIMIT 1",
                    (runner_call_id,)).fetchone() is not None:
                raise ExecutionRecoveryError(
                    f"runner_call {runner_call_id} 已有 ledger，不得 waiver")
            if conn.execute(
                    "SELECT 1 FROM decision WHERE actor='orchestrator' "
                    "AND type='provider_invocation_accounted' AND json_valid(payload_json) "
                    "AND json_extract(payload_json,'$.runner_call_id')=? LIMIT 1",
                    (runner_call_id,)).fetchone() is not None:
                raise ExecutionRecoveryError(
                    f"runner_call {runner_call_id} 已有 provider accounting")
            reconciled = conn.execute(
                "SELECT payload_json FROM decision WHERE actor='orchestrator' "
                "AND type='execution_reconciled' AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.db_owner_kind')='runner_call' "
                "AND json_extract(payload_json,'$.db_owner_id')=? ORDER BY id",
                (runner_call_id,)).fetchall()
            if len(reconciled) != 1:
                raise ExecutionRecoveryError(
                    f"runner_call {runner_call_id} 缺唯一 execution reconciliation")
            reconciliation = json.loads(reconciled[0][0])
            if (reconciliation.get("recovery_action") != "provider_usage_unaccounted"
                    or reconciliation.get("failure_kind") != "cost_accounting"
                    or reconciliation.get("provider_receipt_ref") != invocation.receipt_ref
                    or reconciliation.get("provider_receipt_sha256") != invocation.receipt_sha256
                    or reconciliation.get("receipt_ref") != invocation.execution_receipt_ref
                    or reconciliation.get("receipt_sha256") != invocation.execution_receipt_sha256
                    or reconciliation.get("outcome") != invocation.execution_outcome):
                raise ExecutionRecoveryError(
                    f"runner_call {runner_call_id} reconciliation 与 provider receipt 锚不一致")

            ledger_id = conn.execute(
                "INSERT INTO ledger(cycle_id,phase,runner_call_id,tokens_input,tokens_output,"
                "tokens_total,wallclock_sec,money,policy_version) "
                "VALUES (?,?,?,0,0,0,?,0,?)",
                (cycle_id, phase, runner_call_id,
                 float(invocation.usage.wallclock_sec),
                 self.cost_ledger.policy_version)).lastrowid
            release_payload = {
                "protocol": "global-stop-release-v1",
                "global_stop_id": stop_id,
                "runner_call_id": runner_call_id,
                "reason": "operator_waived_unavailable_usage",
                "provider_receipt_sha256": invocation.receipt_sha256,
            }
            release_id = conn.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (?,'orchestrator','global_stop_release',?)",
                (cycle_id, json.dumps(
                    release_payload, ensure_ascii=False, sort_keys=True))).lastrowid
            waiver_payload = {
                "protocol": "cost-accounting-waiver-v2",
                "runner_call_id": runner_call_id,
                "global_stop_id": stop_id,
                "release_decision_id": release_id,
                "ledger_id": ledger_id,
                "provider_receipt_ref": invocation.receipt_ref,
                "provider_receipt_sha256": invocation.receipt_sha256,
                "execution_receipt_ref": invocation.execution_receipt_ref,
                "execution_receipt_sha256": invocation.execution_receipt_sha256,
                "execution_outcome": invocation.execution_outcome,
                "tokens_known": False,
                "disposition": "zero_charge_unknown_usage",
            }
            waiver_id = conn.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (?,'orchestrator','cost_accounting_waiver_correction',?)",
                (cycle_id, json.dumps(
                    waiver_payload, ensure_ascii=False, sort_keys=True))).lastrowid
        self._validate_terminal_provider(invocation)
        return {
            "waiver_decision_id": int(waiver_id),
            "release_decision_id": int(release_id),
            "ledger_id": int(ledger_id),
        }

    def reconcile_startup(self) -> int:
        owners = self._receipts()
        provider_paths = self._provider_receipts()
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
                provider_path = provider_paths.get(owner_id)
                context = entry[1].get("context") or {}
                if (provider_path is None and row[3] in _ACTIVE_RUNNER
                        and context.get("provider") == "codex-cli"
                        and entry[1].get("capture_stdout_ref") is not None
                        and entry[1].get("capture_error") is None):
                    recovered_provider = reconstruct_provider_invocation_receipt(
                        entry[0], expected_runner_call_id=owner_id,
                        expected_cycle_id=(f"c{row[0]}" if row[0] is not None else ""),
                        expected_phase=row[1], expected_purpose=row[2])
                    provider_path = Path(recovered_provider.receipt_ref)
                    provider_paths[owner_id] = provider_path
                if row[1] in _SPECIALIZED_RUNNER_PHASES:
                    # Deliberate ownership handoff, not a lost ``seen`` row:
                    # Mediator._recover_orphans_once scans active
                    # interaction_query intents directly from DB and atomically
                    # adds its required interaction_reply; ImportSearchService
                    # likewise scans/finalizes its own content receipt.  The
                    # local ``seen`` set never crosses into either service.
                    invocation = (self._load_provider(
                        provider_path, owner_id, row, entry)
                        if provider_path is not None else None)
                    if row[3] not in _ACTIVE_RUNNER and invocation is not None:
                        self._validate_terminal_provider(invocation)
                elif row[3] in _ACTIVE_RUNNER:
                    reconciled += int(self._reconcile_runner(
                        owner_id, row, entry, provider_path))
                elif row[3] == "success":
                    receipt = entry[1]
                    _require_terminal(f"runner_call {owner_id}", receipt)
                    if not (receipt.get("outcome") == "exit" and receipt.get("returncode") == 0):
                        raise ExecutionRecoveryError(
                            f"success runner_call {owner_id} 无 drained exit(0) receipt")
                    if provider_path is not None:
                        self._validate_terminal_provider(self._load_provider(
                            provider_path, owner_id, row, entry))
                elif provider_path is not None:
                    self._validate_terminal_provider(self._load_provider(
                        provider_path, owner_id, row, entry))
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

        for runner_call_id, provider_path in provider_paths.items():
            if ("runner_call", runner_call_id) not in seen:
                raise ExecutionRecoveryError(
                    f"{provider_path.name} 缺可枚举的 execution receipt owner")

        rows = self.daemon.query(
            "SELECT id,cycle_id,phase,purpose,status FROM runner_call "
            "WHERE status IN ('created','running') "
            "AND phase NOT IN ('interaction_query','import_search') ORDER BY id")
        for runner_call_id, cycle_id, phase, purpose, status in rows:
            key = ("runner_call", runner_call_id)
            if key in seen:
                continue
            reconciled += int(self._reconcile_runner(
                runner_call_id, (cycle_id, phase, purpose, status), None,
                provider_paths.get(runner_call_id)))

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
