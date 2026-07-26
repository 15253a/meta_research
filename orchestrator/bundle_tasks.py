"""Recover the one Scheduler task and one stable task per Bundle target.

``runner_call`` is a turn intent, not a Codex task identity.  The durable task
identity is the provider invocation id sealed in the provider receipt after
the guardian has drained that invocation.  This module hides the receipt/SQL
join and exposes one small recovery interface to the stage provider.

It deliberately does not create another session database.  Multiple turns for
the same logical task may have multiple ``runner_call`` rows, but every receipt
must resolve to the same provider task id.  Scheduler, target Workers, and the
fresh code/result review children must remain pairwise distinct.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Set, Tuple

from .provider_invocation import load_provider_invocation_receipt


_CYCLE_ID = re.compile(r"^c([1-9][0-9]*)$")


def _load_verified_native_reviews(conn, *, cycle_id: int):
    """Use the shared durable guardian/owner-proof verifier."""
    from .native_review_verifier import validate_native_reviews
    return validate_native_reviews(conn, cycle_id=cycle_id)


class BundleTaskIdentityError(RuntimeError):
    """Provider receipts cannot prove one unambiguous Bundle task identity."""


@dataclass(frozen=True)
class BundleWorkerTask:
    """Compact durable projection for one target-scoped Worker task."""

    id: int
    cycle_id: int
    build_target_id: int
    provider_task_id: Optional[str]
    status: str
    receipt_ref: Optional[str]


class BundleTaskRegistry:
    """Resolve Bundle task identities from existing accounting receipts."""

    def __init__(
            self, daemon, *,
            receipt_loader: Callable = load_provider_invocation_receipt,
            review_loader: Optional[Callable] = None):
        if daemon is None or not callable(
                getattr(daemon, "query", None)):
            raise ValueError("BundleTaskRegistry 要求可查询的 WriteDaemon")
        if not callable(receipt_loader):
            raise ValueError("receipt_loader 须为 callable")
        if review_loader is not None and not callable(review_loader):
            raise ValueError("review_loader 须为 callable 或 None")
        self.daemon = daemon
        self.receipt_loader = receipt_loader
        self.review_loader = (
            review_loader or _load_verified_native_reviews)

    @staticmethod
    def _requested_key(
            cycle_id: str, *, role: str,
            target_id: Optional[int]) -> Tuple[str, Optional[int]]:
        match = _CYCLE_ID.fullmatch(cycle_id) if isinstance(
            cycle_id, str) else None
        if match is None:
            raise ValueError("cycle_id 须为 c<正整数>")
        if role == "scheduler":
            if target_id is not None:
                raise ValueError("Scheduler task 不得绑定 target_id")
            return ("scheduler", None)
        if role == "target_worker":
            if (isinstance(target_id, bool)
                    or not isinstance(target_id, int)
                    or target_id <= 0):
                raise ValueError("Target Worker task 须绑定正整数 target_id")
            return ("target_worker", target_id)
        raise ValueError("Bundle task role 须为 scheduler 或 target_worker")

    @staticmethod
    def _purpose_key(
            purpose: str, *, cycle_number: int
            ) -> Optional[Tuple[str, Optional[int]]]:
        if not isinstance(purpose, str):
            return None
        scheduler = re.fullmatch(
            rf"bundle-scheduler-c{cycle_number}(?:-.+)?", purpose)
        if scheduler is not None:
            return ("scheduler", None)
        worker = re.fullmatch(
            rf"bundle-worker-c{cycle_number}-t([1-9][0-9]*)(?:-.+)?",
            purpose)
        if worker is not None:
            return ("target_worker", int(worker.group(1)))
        return None

    def _receipt_bindings(
            self, cycle_id: str
            ) -> Tuple[
                Dict[Tuple[str, Optional[int]], Tuple[str, str]],
                Set[Tuple[str, Optional[int]]],
                Set[Tuple[str, Optional[int]]],
            ]:
        """Verify all cycle receipts outside a write transaction.

        The first receipt that proves a provider id is the immutable proof ref
        stored on ``bundle_worker_task``.  Later turns may have new receipts,
        but must prove the same provider id.
        """
        self._requested_key(cycle_id, role="scheduler", target_id=None)
        cycle_number = int(_CYCLE_ID.fullmatch(cycle_id).group(1))
        rows = self.daemon.query(
            "SELECT rc.id,rc.phase,rc.purpose,rc.status,"
            "json_extract(d.payload_json,'$.provider_receipt_ref'),"
            "json_extract(d.payload_json,'$.execution_receipt_ref'),"
            "json_extract(d.payload_json,'$.runner_terminal_status') "
            "FROM runner_call AS rc JOIN decision AS d "
            "ON d.cycle_id=rc.cycle_id "
            "AND d.actor='orchestrator' "
            "AND d.type='provider_invocation_accounted' "
            "AND json_valid(d.payload_json) "
            "AND json_extract(d.payload_json,'$.protocol')="
            "'provider-accounting-v1' "
            "AND json_extract(d.payload_json,'$.runner_call_id')=rc.id "
            "WHERE rc.cycle_id=? AND rc.phase='bundle' "
            "AND rc.status IN ('success','failed') "
            "AND (rc.purpose GLOB ? OR rc.purpose GLOB ?) "
            "ORDER BY rc.id,d.id",
            (
                cycle_number,
                f"bundle-scheduler-c{cycle_number}*",
                f"bundle-worker-c{cycle_number}-t*",
            ),
        )
        by_task: Dict[Tuple[str, Optional[int]], Tuple[str, str]] = {}
        by_provider: Dict[str, Tuple[str, Optional[int]]] = {}
        seen_calls: set[int] = set()
        ambiguous_missing = []
        safe_call_ids: set[int] = set()
        for (runner_call_id, phase, purpose, runner_status,
             provider_ref, execution_ref, accounted_status) in rows:
            if runner_call_id in seen_calls:
                raise BundleTaskIdentityError(
                    f"runner_call {runner_call_id} provider accounting 重复")
            seen_calls.add(int(runner_call_id))
            key = self._purpose_key(
                purpose, cycle_number=cycle_number)
            if key is None:
                raise BundleTaskIdentityError(
                    f"Bundle runner purpose 非法: {purpose!r}")
            if not isinstance(provider_ref, str) or not provider_ref:
                raise BundleTaskIdentityError(
                    f"rc{runner_call_id} provider receipt ref 非法")
            if (not isinstance(execution_ref, str)
                    or not execution_ref):
                raise BundleTaskIdentityError(
                    f"rc{runner_call_id} execution receipt ref 非法")
            if accounted_status != runner_status:
                raise BundleTaskIdentityError(
                    f"rc{runner_call_id} provider accounting terminal "
                    "status 漂移")
            try:
                invocation = self.receipt_loader(
                    Path(provider_ref),
                    expected_runner_call_id=runner_call_id,
                    expected_cycle_id=cycle_id,
                    expected_phase=phase,
                    expected_purpose=purpose,
                    expected_execution_receipt_ref=execution_ref,
                )
            except Exception as error:
                raise BundleTaskIdentityError(
                    f"Bundle provider receipt 不可复验: rc{runner_call_id}"
                ) from error
            provider_id = getattr(
                invocation, "provider_invocation_id", None)
            if provider_id is None:
                safe_pre_session_exit = (
                    getattr(invocation, "execution_outcome", None) == "exit"
                    and isinstance(
                        getattr(invocation, "execution_returncode", None), int)
                    and invocation.execution_returncode != 0
                )
                if not safe_pre_session_exit:
                    ambiguous_missing.append(int(runner_call_id))
                else:
                    safe_call_ids.add(int(runner_call_id))
                continue
            if not isinstance(provider_id, str) or not provider_id:
                raise BundleTaskIdentityError(
                    f"rc{runner_call_id} provider task identity 非法")
            prior = by_task.get(key)
            if prior is not None and prior[0] != provider_id:
                raise BundleTaskIdentityError(
                    f"{key} provider task identity 漂移: "
                    f"{prior[0]!r} != {provider_id!r}")
            other = by_provider.get(provider_id)
            if other is not None and other != key:
                raise BundleTaskIdentityError(
                    "多个 Bundle task 共享同一 provider task identity: "
                    f"{other} / {key} -> {provider_id!r}")
            if prior is None:
                by_task[key] = (provider_id, provider_ref)
            by_provider[provider_id] = key
        if ambiguous_missing:
            raise BundleTaskIdentityError(
                "Bundle 历史调用缺可证明的 provider task identity: "
                + ",".join(f"rc{item}" for item in ambiguous_missing))
        latest_calls: Dict[
            Tuple[str, Optional[int]], Tuple[int, str]] = {}
        for runner_call_id, purpose, status in self.daemon.query(
                "SELECT id,purpose,status FROM runner_call "
                "WHERE cycle_id=? AND phase='bundle' "
                "AND (purpose GLOB ? OR purpose GLOB ?) ORDER BY id",
                (
                    cycle_number,
                    f"bundle-scheduler-c{cycle_number}*",
                    f"bundle-worker-c{cycle_number}-t*",
                )):
            key = self._purpose_key(
                purpose, cycle_number=cycle_number)
            if key is None:
                raise BundleTaskIdentityError(
                    f"Bundle runner purpose 非法: {purpose!r}")
            latest_calls[key] = (int(runner_call_id), str(status))
        safe_missing: Set[Tuple[str, Optional[int]]] = set()
        unresolved: Set[Tuple[str, Optional[int]]] = set()
        for key, (runner_call_id, status) in latest_calls.items():
            if runner_call_id in safe_call_ids or status == "aborted":
                safe_missing.add(key)
            elif runner_call_id not in seen_calls:
                unresolved.add(key)
        return by_task, safe_missing, unresolved

    def recover(
            self, cycle_id: str, *, role: str,
            target_id: Optional[int] = None) -> Optional[str]:
        """Return the exact provider task id, ``None`` when never started.

        All Scheduler/Worker receipts in the cycle are checked on every call,
        so asking for target A cannot overlook a provider id already reused by
        target B.  A normal pre-session non-zero exit may honestly have no
        provider id; ambiguous missing ids fail closed.
        """
        requested = self._requested_key(
            cycle_id, role=role, target_id=target_id)
        bindings, _safe_missing, unresolved = self._receipt_bindings(cycle_id)
        if requested in unresolved:
            raise BundleTaskIdentityError(
                f"{requested} provider 调用未收口")
        binding = bindings.get(requested)
        return binding[0] if binding is not None else None

    @staticmethod
    def _worker_projection(row) -> BundleWorkerTask:
        return BundleWorkerTask(
            id=int(row[0]),
            cycle_id=int(row[1]),
            build_target_id=int(row[2]),
            provider_task_id=row[3],
            status=str(row[4]),
            receipt_ref=row[5],
        )

    @staticmethod
    def _select_worker(conn, *, cycle_number: int, target_id: int):
        return conn.execute(
            "SELECT id,cycle_id,build_target_id,provider_task_id,status,"
            "receipt_ref FROM bundle_worker_task "
            "WHERE cycle_id=? AND build_target_id=? AND role='worker'",
            (cycle_number, target_id),
        ).fetchone()

    @staticmethod
    def _merge_verified_binding(
            conn, row, *,
            binding: Optional[Tuple[str, str]]) -> None:
        if binding is None:
            if ((row[3] is None) != (row[5] is None)):
                raise BundleTaskIdentityError(
                    "Worker task provider identity/ref 必须成对存在")
            if row[3] is not None:
                raise BundleTaskIdentityError(
                    "Worker task 已有 provider identity 但缺可复验 receipt")
            return
        provider_task_id, receipt_ref = binding
        if row[3] is not None and row[3] != provider_task_id:
            raise BundleTaskIdentityError(
                "Worker task 已证明的 provider identity 不得覆盖")
        if row[5] is not None and row[5] != receipt_ref:
            raise BundleTaskIdentityError(
                "Worker task 已证明的 provider receipt 不得覆盖")
        conn.execute(
            "UPDATE bundle_worker_task SET "
            "provider_task_id=COALESCE(provider_task_id,?),"
            "receipt_ref=COALESCE(receipt_ref,?),"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (provider_task_id, receipt_ref, row[0]),
        )

    @staticmethod
    def _strict_object(raw, *, label: str) -> Dict:
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise BundleTaskIdentityError(
                f"{label} JSON 不可复验") from error
        if not isinstance(value, dict):
            raise BundleTaskIdentityError(f"{label} 须为 object")
        return value

    def _verified_review_bindings(
            self, cycle_id: str, *, target_id: int
            ) -> Dict[str, Tuple[str, str]]:
        """Resolve the final code/result child selected by runtime authority."""
        cycle_number = int(_CYCLE_ID.fullmatch(cycle_id).group(1))
        try:
            validated = self.review_loader(
                self.daemon.conn, cycle_id=cycle_number)
        except Exception as error:
            raise BundleTaskIdentityError(
                f"cycle {cycle_id} native review authority 不可复验"
            ) from error
        validated_by_id = {}
        child_owners = {}
        for item in validated:
            if (
                    not isinstance(item, tuple)
                    or len(item) != 2
                    or isinstance(item[0], bool)
                    or not isinstance(item[0], int)
                    or not isinstance(item[1], dict)
                    or item[0] in validated_by_id):
                raise BundleTaskIdentityError(
                    "native review verifier 返回非法/重复项")
            validated_by_id[item[0]] = item[1]
            child_thread_id = item[1].get("child_thread_id")
            if not isinstance(child_thread_id, str) or not child_thread_id:
                raise BundleTaskIdentityError(
                    "native review child task identity 非法")
            prior_owner = child_owners.get(child_thread_id)
            if prior_owner is not None and prior_owner != item[0]:
                raise BundleTaskIdentityError(
                    "native review child task 被多个 review 复用: "
                    f"{child_thread_id!r}")
            child_owners[child_thread_id] = item[0]
        selectors = {}
        selector_specs = {
            "code_review": (
                "agent", "runtime_stage_submission",
                "runtime-stage-submission-index-v1",
                "target_id", "bundle_code"),
            "result_review": (
                "orchestrator", "runtime_bundle_result_review_ack",
                "native-bundle-result-review-ack-v2",
                "build_target_id", "bundle_result"),
        }
        for role, (
                actor, decision_type, protocol, target_field,
                review_kind) in selector_specs.items():
            rows = self.daemon.query(
                "SELECT id,payload_json FROM decision "
                "WHERE cycle_id=? AND actor=? AND type=? "
                "AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.protocol')=? "
                f"AND CAST(json_extract(payload_json,'$.{target_field}') "
                "AS TEXT)=? ORDER BY id",
                (
                    cycle_number, actor, decision_type, protocol,
                    str(target_id),
                ),
            )
            if not rows:
                raise BundleTaskIdentityError(
                    f"target {target_id} 缺最终 {role} authority selector")
            selector = self._strict_object(
                rows[-1][1], label=f"{role} selector")
            review_id = selector.get("review_decision_id")
            if (isinstance(review_id, bool)
                    or not isinstance(review_id, int)
                    or review_id <= 0):
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} selector 缺 review decision")
            selectors[role] = (review_id, review_kind, selector)

        bindings: Dict[str, Tuple[str, str]] = {}
        for role, (
                review_id, expected_kind, selector) in selectors.items():
            row = self.daemon.query_one(
                "SELECT actor,type,payload_json FROM decision "
                "WHERE id=? AND cycle_id=?",
                (review_id, cycle_number),
            )
            if row is None or row[:2] != ("agent", "runtime_review"):
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} review decision 非权威")
            receipt = self._strict_object(
                row[2], label=f"{role} runtime review")
            if (
                    validated_by_id.get(review_id) != receipt
                    or receipt.get("protocol")
                    != "native-review-receipt-v1"
                    or receipt.get("cycle_id") != cycle_id
                    or receipt.get("stage") != "bundle"
                    or str(receipt.get("target_id")) != str(target_id)
                    or receipt.get("review_kind") != expected_kind
                    or receipt.get("verdict") != "pass"
                    or receipt.get("round_no")
                    != receipt.get("configured_rounds")):
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} review receipt 不可复验")
            if (
                    (role == "code_review"
                     and selector.get("artifact_hash")
                     != receipt.get("resulting_subject_hash"))
                    or (
                        role == "result_review"
                        and (
                            selector.get("review_receipt_hash")
                            != receipt.get("receipt_hash")
                            or selector.get("subject_hash")
                            != receipt.get("resulting_subject_hash")))):
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} selector/receipt 漂移")
            runner_call_id = receipt.get("runner_call_id")
            if (isinstance(runner_call_id, bool)
                    or not isinstance(runner_call_id, int)
                    or runner_call_id <= 0):
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} runner_call 非法")
            parent = self.daemon.query_one(
                "SELECT cycle_id,phase,purpose FROM runner_call WHERE id=?",
                (runner_call_id,),
            )
            expected_worker = ("target_worker", target_id)
            if (
                    parent is None
                    or parent[0] != cycle_number
                    or parent[1] != "bundle"
                    or self._purpose_key(
                        parent[2], cycle_number=cycle_number)
                    != expected_worker
                    or receipt.get("purpose") != parent[2]):
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} 未绑定本 Worker call")
            child_thread_id = receipt.get("child_thread_id")
            if (not isinstance(child_thread_id, str)
                    or not child_thread_id
                    or len(child_thread_id) > 4096):
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} child task identity 非法")
            accounting_rows = self.daemon.query(
                "SELECT payload_json FROM decision "
                "WHERE cycle_id=? AND actor='orchestrator' "
                "AND type='provider_invocation_accounted' "
                "AND json_valid(payload_json) "
                "AND json_extract(payload_json,"
                "'$.runner_call_id')=? ORDER BY id",
                (cycle_number, runner_call_id),
            )
            if len(accounting_rows) != 1:
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} provider accounting 非唯一")
            accounting = self._strict_object(
                accounting_rows[0][0], label=f"{role} provider accounting")
            provider_receipt_ref = accounting.get(
                "provider_receipt_ref")
            if (
                    accounting.get("protocol")
                    != "provider-accounting-v1"
                    or accounting.get("runner_call_id") != runner_call_id
                    or accounting.get("runner_terminal_status") != "success"
                    or not isinstance(provider_receipt_ref, str)
                    or not provider_receipt_ref
                    or len(provider_receipt_ref) > 4096):
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} provider accounting 不可复验")
            bindings[role] = (
                child_thread_id, provider_receipt_ref)
        if bindings["code_review"][0] == bindings["result_review"][0]:
            raise BundleTaskIdentityError(
                f"target {target_id} code/result review 不得共享 child task")
        return bindings

    @staticmethod
    def _merge_review_bindings(
            conn, *, cycle_number: int, target_id: int,
            bindings: Dict[str, Tuple[str, str]]) -> None:
        for role, (provider_task_id, receipt_ref) in bindings.items():
            other = conn.execute(
                "SELECT build_target_id,role FROM bundle_worker_task "
                "WHERE provider_task_id=? AND NOT ("
                "build_target_id=? AND role=?)",
                (provider_task_id, target_id, role),
            ).fetchone()
            if other is not None:
                raise BundleTaskIdentityError(
                    "review child task identity 被跨 target/role 复用: "
                    f"{provider_task_id!r}")
            row = conn.execute(
                "SELECT id,cycle_id,provider_task_id,status,receipt_ref "
                "FROM bundle_worker_task WHERE build_target_id=? AND role=?",
                (target_id, role),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO bundle_worker_task("
                    "build_target_id,cycle_id,role,provider_task_id,status,"
                    "receipt_ref) VALUES (?,?,?,?, 'completed',?)",
                    (
                        target_id, cycle_number, role, provider_task_id,
                        receipt_ref,
                    ),
                )
                continue
            if (
                    row[1] != cycle_number
                    or (row[2] is not None
                        and row[2] != provider_task_id)
                    or (row[4] is not None and row[4] != receipt_ref)):
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} 已证明 identity 不得漂移")
            if row[3] != "completed":
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} ledger 非 completed")
            conn.execute(
                "UPDATE bundle_worker_task SET "
                "provider_task_id=COALESCE(provider_task_id,?),"
                "receipt_ref=COALESCE(receipt_ref,?),status='completed',"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (provider_task_id, receipt_ref, row[0]),
            )

    def prepare_worker(
            self, cycle_id: str, *, target_id: int) -> BundleWorkerTask:
        """Create/recover the one Worker row before constructing its turn."""
        requested = self._requested_key(
            cycle_id, role="target_worker", target_id=target_id)
        cycle_number = int(_CYCLE_ID.fullmatch(cycle_id).group(1))
        bindings, _safe_missing, unresolved = self._receipt_bindings(cycle_id)
        if requested in unresolved:
            raise BundleTaskIdentityError(
                f"c{cycle_number}/target {target_id} provider 调用未收口")
        binding = bindings.get(requested)
        with self.daemon.transaction() as conn:
            target = conn.execute(
                "SELECT id FROM build_target WHERE id=? AND cycle_id=?",
                (target_id, cycle_number),
            ).fetchone()
            if target is None:
                raise BundleTaskIdentityError(
                    f"c{cycle_number}/target {target_id} 不存在")
            conn.execute(
                "INSERT INTO bundle_worker_task("
                "build_target_id,cycle_id,role,status) "
                "VALUES (?,?,'worker','created') "
                "ON CONFLICT(build_target_id,role) DO NOTHING",
                (target_id, cycle_number),
            )
            row = self._select_worker(
                conn, cycle_number=cycle_number, target_id=target_id)
            if row is None:
                raise BundleTaskIdentityError("Worker task 创建后不可见")
            self._merge_verified_binding(conn, row, binding=binding)
            row = self._select_worker(
                conn, cycle_number=cycle_number, target_id=target_id)
            if row[4] == "completed":
                raise BundleTaskIdentityError(
                    f"c{cycle_number}/target {target_id} Worker 已完成")
            if (row[4] in {"running", "waiting"}
                    and row[3] is None):
                raise BundleTaskIdentityError(
                    f"c{cycle_number}/target {target_id} Worker 中断但缺"
                    "可续接 provider identity")
            if row[4] != "created":
                conn.execute(
                    "UPDATE bundle_worker_task SET status='created',"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row[0],),
                )
                row = self._select_worker(
                    conn, cycle_number=cycle_number, target_id=target_id)
            return self._worker_projection(row)

    def mark_worker_running(
            self, cycle_id: str, *, target_id: int) -> BundleWorkerTask:
        """Transition the prepared Worker immediately before provider I/O."""
        self._requested_key(
            cycle_id, role="target_worker", target_id=target_id)
        cycle_number = int(_CYCLE_ID.fullmatch(cycle_id).group(1))
        with self.daemon.transaction() as conn:
            changed = conn.execute(
                "UPDATE bundle_worker_task SET status='running',"
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE cycle_id=? AND build_target_id=? AND role='worker' "
                "AND status='created'",
                (cycle_number, target_id),
            ).rowcount
            if changed != 1:
                raise BundleTaskIdentityError(
                    f"c{cycle_number}/target {target_id} Worker 非 created")
            row = self._select_worker(
                conn, cycle_number=cycle_number, target_id=target_id)
            return self._worker_projection(row)

    def mark_worker_completed(
            self, cycle_id: str, *, target_id: int) -> BundleWorkerTask:
        """Seal a Worker against its durable target terminal and receipts."""
        requested = self._requested_key(
            cycle_id, role="target_worker", target_id=target_id)
        cycle_number = int(_CYCLE_ID.fullmatch(cycle_id).group(1))
        target = self.daemon.query_one(
            "SELECT status FROM build_target WHERE id=? AND cycle_id=?",
            (target_id, cycle_number),
        )
        if target is None or target[0] not in {
                "complete", "failed", "skipped",
                "engineering_blocked"}:
            raise BundleTaskIdentityError(
                f"c{cycle_number}/target {target_id} 缺 durable terminal")
        target_status = str(target[0])
        bindings, _safe_missing, unresolved = self._receipt_bindings(cycle_id)
        if requested in unresolved:
            raise BundleTaskIdentityError(
                f"c{cycle_number}/target {target_id} provider 调用未收口")
        binding = bindings.get(requested)
        if binding is None:
            raise BundleTaskIdentityError(
                f"c{cycle_number}/target {target_id} 完成但缺 provider identity")
        review_bindings = (
            self._verified_review_bindings(
                cycle_id, target_id=target_id)
            if target_status == "complete" else {})
        provider_task_ids = {
            provider_task_id
            for provider_task_id, _receipt_ref in bindings.values()
        }
        for role, (review_task_id, _receipt_ref) in review_bindings.items():
            if review_task_id in provider_task_ids:
                raise BundleTaskIdentityError(
                    f"target {target_id} {role} 复用 Scheduler/Worker task")
        with self.daemon.transaction() as conn:
            current_target = conn.execute(
                "SELECT status FROM build_target "
                "WHERE id=? AND cycle_id=?",
                (target_id, cycle_number),
            ).fetchone()
            if current_target != (target_status,):
                raise BundleTaskIdentityError(
                    f"c{cycle_number}/target {target_id} terminal 漂移")
            row = self._select_worker(
                conn, cycle_number=cycle_number, target_id=target_id)
            if row is None or row[4] != "running":
                raise BundleTaskIdentityError(
                    f"c{cycle_number}/target {target_id} Worker 非 running")
            self._merge_verified_binding(conn, row, binding=binding)
            if review_bindings:
                self._merge_review_bindings(
                    conn, cycle_number=cycle_number,
                    target_id=target_id, bindings=review_bindings)
            conn.execute(
                "UPDATE bundle_worker_task SET status='completed',"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (row[0],),
            )
            row = self._select_worker(
                conn, cycle_number=cycle_number, target_id=target_id)
            return self._worker_projection(row)

    def reconcile_terminal_workers(
            self, cycle_id: str) -> Tuple[BundleWorkerTask, ...]:
        """Close terminal Worker crash windows from durable proof only.

        A Worker row is created before provider I/O.  Therefore a terminal
        target with a receipt-backed Worker call but no existing Worker row is
        corruption, not permission to manufacture a replacement ledger row.
        Existing completed rows are deliberately walked through the same
        receipt and review verification on every Scheduler boundary.
        """
        self._requested_key(
            cycle_id, role="scheduler", target_id=None)
        cycle_number = int(_CYCLE_ID.fullmatch(cycle_id).group(1))
        bindings, safe_missing, unresolved = self._receipt_bindings(cycle_id)
        terminal_rows = self.daemon.query(
            "SELECT bt.id,bt.status,w.id,w.cycle_id,w.build_target_id,"
            "w.provider_task_id,w.status,w.receipt_ref "
            "FROM build_target AS bt "
            "LEFT JOIN bundle_worker_task AS w "
            "ON w.build_target_id=bt.id AND w.role='worker' "
            "WHERE bt.cycle_id=? "
            "AND bt.status IN ("
            "'complete','failed','skipped','engineering_blocked') "
            "ORDER BY bt.seq,bt.id",
            (cycle_number,),
        )
        provider_task_ids = {
            provider_task_id
            for provider_task_id, _receipt_ref in bindings.values()
        }
        reconciled = []
        for row in terminal_rows:
            target_id = int(row[0])
            target_status = str(row[1])
            requested = ("target_worker", target_id)
            binding = bindings.get(requested)
            has_call_evidence = (
                binding is not None
                or requested in safe_missing
                or requested in unresolved)
            worker_row = None if row[2] is None else (
                row[2], row[3], row[4], row[5], row[6], row[7])
            if worker_row is None:
                if has_call_evidence:
                    raise BundleTaskIdentityError(
                        f"c{cycle_number}/target {target_id} "
                        "缺既有 Worker task；禁止恢复时合成")
                # A never-dispatched skipped descendant has no Worker ledger.
                continue
            if requested in unresolved:
                raise BundleTaskIdentityError(
                    f"c{cycle_number}/target {target_id} provider 调用未收口")
            if binding is None:
                raise BundleTaskIdentityError(
                    f"c{cycle_number}/target {target_id} terminal Worker "
                    "缺可复验 provider identity")
            if worker_row[4] not in {"running", "waiting", "completed"}:
                raise BundleTaskIdentityError(
                    f"c{cycle_number}/target {target_id} terminal Worker "
                    f"状态不可恢复: {worker_row[4]!r}")
            review_bindings = (
                self._verified_review_bindings(
                    cycle_id, target_id=target_id)
                if target_status == "complete" else {})
            for role, (
                    review_task_id, _receipt_ref) in review_bindings.items():
                if review_task_id in provider_task_ids:
                    raise BundleTaskIdentityError(
                        f"target {target_id} {role} "
                        "复用 Scheduler/Worker task")
            with self.daemon.transaction() as conn:
                current_target = conn.execute(
                    "SELECT status FROM build_target "
                    "WHERE id=? AND cycle_id=?",
                    (target_id, cycle_number),
                ).fetchone()
                if current_target != (target_status,):
                    raise BundleTaskIdentityError(
                        f"c{cycle_number}/target {target_id} terminal 漂移")
                current = self._select_worker(
                    conn, cycle_number=cycle_number,
                    target_id=target_id)
                if current is None:
                    raise BundleTaskIdentityError(
                        f"c{cycle_number}/target {target_id} "
                        "Worker task 在复验期间消失")
                if current[4] not in {
                        "running", "waiting", "completed"}:
                    raise BundleTaskIdentityError(
                        f"c{cycle_number}/target {target_id} terminal "
                        f"Worker 状态漂移: {current[4]!r}")
                self._merge_verified_binding(
                    conn, current, binding=binding)
                if review_bindings:
                    self._merge_review_bindings(
                        conn, cycle_number=cycle_number,
                        target_id=target_id,
                        bindings=review_bindings)
                conn.execute(
                    "UPDATE bundle_worker_task SET status='completed',"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (current[0],),
                )
                current = self._select_worker(
                    conn, cycle_number=cycle_number,
                    target_id=target_id)
                reconciled.append(self._worker_projection(current))
        return tuple(reconciled)

    def mark_worker_interrupted(
            self, cycle_id: str, *, target_id: int) -> BundleWorkerTask:
        """Keep an interrupted provider turn recoverable without identity loss.

        A verified provider id means a later turn may resume the same task, so
        the honest state is ``waiting``.  A receipt proving that the provider
        process exited before creating any provider task is ``failed`` and may
        be retried from ``created``.  Missing or ambiguous proof is also
        ``waiting`` (fail closed against silently creating a replacement).
        """
        requested = self._requested_key(
            cycle_id, role="target_worker", target_id=target_id)
        cycle_number = int(_CYCLE_ID.fullmatch(cycle_id).group(1))
        binding = None
        safe_missing: Set[Tuple[str, Optional[int]]] = set()
        unresolved: Set[Tuple[str, Optional[int]]] = set()
        try:
            bindings, safe_missing, unresolved = (
                self._receipt_bindings(cycle_id))
            binding = bindings.get(requested)
        except BundleTaskIdentityError:
            # Preserve the original provider/network exception at the caller.
            # The next prepare will re-run strict receipt verification and
            # fail closed; this transition only records that work is resumable.
            pass
        with self.daemon.transaction() as conn:
            row = self._select_worker(
                conn, cycle_number=cycle_number, target_id=target_id)
            if row is None:
                raise BundleTaskIdentityError(
                    f"c{cycle_number}/target {target_id} Worker task 不存在")
            if row[4] == "completed":
                return self._worker_projection(row)
            if binding is not None:
                self._merge_verified_binding(conn, row, binding=binding)
                status = "waiting"
            elif row[4] == "created":
                status = "failed"
            elif requested in unresolved:
                status = "waiting"
            elif requested in safe_missing:
                status = "failed"
            else:
                status = "waiting"
            conn.execute(
                "UPDATE bundle_worker_task SET status=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, row[0]),
            )
            row = self._select_worker(
                conn, cycle_number=cycle_number, target_id=target_id)
            return self._worker_projection(row)
