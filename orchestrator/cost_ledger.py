"""CostLedger —— LLM 调用成本记账（步⑩ M6 CP10.2）。

每次可知用量的真 LLM(Codex) 调用后写一行 `ledger`（+ 需要时补 `runner_call`），把 CP10.1 捕获的真用量（token/墙钟）
折成 `money = tokens/1000 × policy.budget.price_per_1k_tokens`。这**激活**了 `stopcontroller` 早已装好的全局
预算安全网（`SUM(ledger.money) ≥ session_max` → `budget_exhausted` 干净停）——在此之前 ledger 无写者、SUM 恒 0、网休眠。
用量缺失/不可信时不伪造零成本 ledger：预算开启则落 failed runner_call + durable
`cost_accounting_failed` 并立即停；只有 `session_max=null` 的显式诊断模式允许未知按零 best-effort 审计。

**两个写法**（因 runner_call 归属不同）：
- `record(...)`：StageProvider 用——idea/plan/bundle/reasoning 阶段**原本不写 runner_call**，故自开短 txn 先建
  runner_call 再写 ledger，返回 runner_call_id。
- `insert_ledger_for_runner(...)`：JudgeProvider 在其现有短 txn 内调用，使最终有效裁决的
  runner_call + ledger + DECISION 同生共死；`record_ledger_only(...)` 保留给已有 runner_call 的独立补账。

**预算启用时 fail-closed**：最新耐久预算投影的 `session_max != null` 表示成本护栏是运行契约，任何记账失败都必须中止推进，不能
继续制造不可见成本；只有明确以 `session_max=null` 关闭护栏时，调用方才可 best-effort 记录。
ledger append-only（DDL 触发器）→ 累计靠新 INSERT，本类只 INSERT、从不 UPDATE。
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Optional

from .ids import cnum as _cnum
from .interfaces import CallUsage
from .provider_invocation import (ProviderInvocation, load_provider_invocation_receipt,
                                  provider_receipt_for_execution)
from .runtime_control import (effective_budget_config, policy_with_effective_budget,
                              validate_budget_config)

_SQLITE_INT_MAX = (1 << 63) - 1


def policy_fingerprint(policy: dict) -> str:
    """整份 policy 的规范化 JSON sha256；ledger.policy_version 由内容派生，不引入可手填版本旋钮。"""
    try:
        canonical = json.dumps(policy, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ValueError(f"policy 无法规范化为有限 JSON：{e}") from e
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BudgetExhausted(RuntimeError):
    """本次成本已提交且触发 durable global_stop；调用方必须立即停止后续 LLM 调用。"""

    def __init__(self, *, spent: float, session_max: float):
        self.spent = spent
        self.session_max = session_max
        super().__init__(f"budget_exhausted: spent={spent} >= session_max={session_max}")


class CostAccountingFailed(RuntimeError):
    """成本无法可信计算；失败事实与 durable stop 已尽力提交。"""

    def __init__(self, message: str, *, runner_call_id: Optional[int] = None):
        self.runner_call_id = runner_call_id
        super().__init__(message)


class CostLedger:
    """成本记账写入器（daemon 单写者内；见模块 docstring）。"""

    def __init__(self, daemon, policy: dict):
        self.daemon = daemon
        self.policy = policy
        self._base_budget = dict(policy.get("budget") or {})
        cfg = self.validate_policy(policy)
        self.budget_enabled = cfg["budget_enabled"]
        self.session_max = cfg["session_max"]
        self.price_per_1k = cfg["price_per_1k"]
        self.policy_version = cfg["policy_version"]

    @classmethod
    def validate_policy(cls, policy: dict) -> dict:
        """纯预验证成本配置；build_system 在开 DB 前调用，避免半初始化后才暴露溢出/类型错误。"""
        budget = policy.get("budget") or {}
        if "session_max" not in budget:
            raise ValueError("budget.session_max 必须显式存在（有限正数启用，null 关闭）")
        raw_session_max = budget.get("session_max")
        budget_enabled = raw_session_max is not None
        session_max = None
        if budget_enabled:
            if isinstance(raw_session_max, bool):
                raise ValueError("budget.session_max 必须是有限正数或 null")
            try:
                session_max = float(raw_session_max)
            except (OverflowError, TypeError, ValueError) as e:
                raise ValueError("budget.session_max 必须是有限正数或 null") from e
            if not math.isfinite(session_max) or session_max <= 0:
                raise ValueError("budget.session_max 必须是有限正数或 null")
        raw_price = budget.get("price_per_1k_tokens", 0.0)
        if isinstance(raw_price, bool):
            raise ValueError(f"price_per_1k_tokens 必须是有限数字，实收 {raw_price!r}")
        try:
            price_per_1k = float(raw_price)
        except (OverflowError, TypeError, ValueError) as e:
            raise ValueError(f"price_per_1k_tokens 必须是有限数字，实收 {raw_price!r}") from e
        if not math.isfinite(price_per_1k) or price_per_1k < 0:
            raise ValueError(f"price_per_1k_tokens 必须是有限非负数，实收 {raw_price!r}")
        # session_max=null 是关闭预算网的唯一显式方式；网已开却 price=0 会让 SUM 永不增长，必须启动失败。
        if budget_enabled and price_per_1k <= 0:
            raise ValueError("budget.session_max 已启用时 price_per_1k_tokens 必须是有限正数")
        return {"budget_enabled": budget_enabled, "session_max": session_max,
                "price_per_1k": price_per_1k, "policy_version": policy_fingerprint(policy)}

    def money_for(self, usage: Optional[CallUsage], *, budget_enabled: Optional[bool] = None) -> float:
        """token → money。未知用量：预算开启时拒绝，显式关闭时才按 0 best-effort。"""
        u = self._validated_usage(usage)
        enabled = self.budget_enabled if budget_enabled is None else budget_enabled
        if enabled and not u.tokens_known:
            raise ValueError("token 汇总未知，预算启用时不能按真 0 记账")
        try:
            money = (u.tokens_total / 1000.0) * self.price_per_1k
        except (OverflowError, ValueError) as e:
            raise ValueError("tokens_total × price_per_1k_tokens 无法表示为有限成本") from e
        if not math.isfinite(money) or money < 0:
            raise ValueError(f"计算出的 money 必须有限非负，实收 {money!r}")
        if u.tokens_total > 0 and self.price_per_1k > 0 and money == 0:
            raise ValueError("正 tokens_total 的成本下溢为 0；price_per_1k_tokens 过小")
        # 不固定小数位 round：SQLite REAL 可保存极小正成本，逐次舍入为 0 会系统性欠计。
        return money

    def new_external_call_block_reason(self, conn=None) -> Optional[str]:
        """Return the durable/effective cost reason that forbids starting another external call.

        Do not rely only on the single ``global_stop`` row: a prior score-floor stop may already occupy that
        append-only slot when a later interaction call crosses budget or loses its usage receipt.
        """
        if conn is None:
            # A consistent check is useful to callers, but only a caller that
            # also creates its intent in this same transaction closes the
            # check-to-start race (Mediator does so).
            with self.daemon.transaction() as locked:
                return self.new_external_call_block_reason(locked)
        effective = effective_budget_config(
            conn, self._base_budget, require_schedule=False)
        if effective["session_max"] is None:
            return None
        explicit = conn.execute(
            "SELECT json_extract(payload_json,'$.reason') FROM decision "
            "WHERE actor='orchestrator' AND type='global_stop' "
            "AND json_extract(payload_json,'$.reason') IN ('budget_exhausted','cost_accounting_failed') "
            "ORDER BY id LIMIT 1").fetchone()
        if explicit is not None:
            return str(explicit[0])
        unaccounted = conn.execute(
            "SELECT 1 FROM runner_call rc WHERE rc.status='failed' "
            "AND rc.failure_kind IN ('cost_accounting','orphaned_query_intent') "
            "AND NOT EXISTS (SELECT 1 FROM ledger l WHERE l.runner_call_id=rc.id) LIMIT 1").fetchone()
        if unaccounted is not None:
            return "cost_accounting_failed"
        spent = float(conn.execute("SELECT COALESCE(SUM(money),0) FROM ledger").fetchone()[0])
        if spent >= effective["session_max"]:
            return "budget_exhausted"
        return None

    @staticmethod
    def _validated_usage(usage: Optional[CallUsage]) -> CallUsage:
        """校验并规范化一次调用用量，拒绝 bool/负数/NaN/Inf，避免污染 append-only ledger。"""
        u = usage if usage is not None else CallUsage(tokens_known=False)
        known = getattr(u, "tokens_known", None)
        if not isinstance(known, bool):
            raise ValueError(f"CallUsage.tokens_known 必须是 bool，实收 {known!r}")
        values = {}
        for name in ("tokens_input", "tokens_output", "tokens_total"):
            value = getattr(u, name, None)
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < 0 or value > _SQLITE_INT_MAX):
                raise ValueError(f"CallUsage.{name} 必须是 0..{_SQLITE_INT_MAX} 的整数，实收 {value!r}")
            values[name] = value
        if not known and any(values.values()):
            raise ValueError("CallUsage.tokens_known=false 时 token 字段必须全为 0")
        if (values["tokens_input"] or values["tokens_output"]) and (
                values["tokens_total"] < values["tokens_input"] + values["tokens_output"]):
            raise ValueError("CallUsage.tokens_total 不得小于 tokens_input+tokens_output")
        wallclock = getattr(u, "wallclock_sec", None)
        if isinstance(wallclock, bool) or not isinstance(wallclock, (int, float)):
            raise ValueError(f"CallUsage.wallclock_sec 必须是有限非负数，实收 {wallclock!r}")
        try:
            wallclock_f = float(wallclock)
        except (OverflowError, TypeError, ValueError) as e:
            raise ValueError(f"CallUsage.wallclock_sec 必须是有限非负数，实收 {wallclock!r}") from e
        if not math.isfinite(wallclock_f) or wallclock_f < 0:
            raise ValueError(f"CallUsage.wallclock_sec 必须是有限非负数，实收 {wallclock!r}")
        return CallUsage(wallclock_sec=wallclock_f, tokens_known=known, **values)

    def record(self, *, cycle_id: str, phase: str, purpose: str,
               usage: Optional[CallUsage], status: str = "success",
               failure_kind: Optional[str] = None) -> int:
        """兼容入口：以 created→running→terminal 三步记录一次已发生调用。"""
        rc = self.begin_call(cycle_id=cycle_id, phase=phase, purpose=purpose)
        self.mark_call_running(runner_call_id=rc)
        self.finish_call(
            runner_call_id=rc, status=status, usage=usage,
            failure_kind=failure_kind)
        return rc

    def begin_call(self, *, cycle_id: str, phase: str, purpose: str,
                   transcript_ref: Optional[str] = None) -> int:
        """在外部调用前耐久化 created intent，并在同一事务执行成本停止闸。"""
        ci = _cnum(cycle_id)
        with self.daemon.transaction() as conn:
            blocked = self.new_external_call_block_reason(conn)
            if blocked == "budget_exhausted":
                effective = effective_budget_config(
                    conn, self._base_budget, require_schedule=False)
                spent = float(conn.execute(
                    "SELECT COALESCE(SUM(money),0) FROM ledger").fetchone()[0])
                raise BudgetExhausted(spent=spent, session_max=float(effective["session_max"]))
            if blocked is not None:
                raise CostAccountingFailed(f"新外部调用被 durable 成本闸阻断: {blocked}")
            return conn.execute(
                "INSERT INTO runner_call(cycle_id,phase,purpose,status,transcript_ref) "
                "VALUES (?,?,?,'created',?)",
                (ci, phase, purpose, transcript_ref)).lastrowid

    def mark_call_running(self, *, runner_call_id: int) -> None:
        """created intent 在最后外部调用边界前迁入 running。"""
        with self.daemon.transaction() as conn:
            changed = conn.execute(
                "UPDATE runner_call SET status='running',started_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='created'", (runner_call_id,)).rowcount
            if changed != 1:
                row = conn.execute(
                    "SELECT status FROM runner_call WHERE id=?", (runner_call_id,)).fetchone()
                raise RuntimeError(
                    f"runner_call {runner_call_id} 不可 start（status={row[0] if row else 'missing'}）")

    def abort_created_call(self, *, runner_call_id: int, failure_kind: str) -> None:
        """外部调用尚未开始时终结 intent；不写 ledger，因为没有调用成本。"""
        self.abort_unstarted_call(
            runner_call_id=runner_call_id, failure_kind=failure_kind,
            allowed_statuses=("created",))

    def abort_unstarted_call(self, *, runner_call_id: int, failure_kind: str,
                             allowed_statuses=("created", "running")) -> None:
        """由调用边界证明尚未执行时终结 created/running intent，不伪造成本。"""
        if not isinstance(failure_kind, str) or not failure_kind.strip():
            raise ValueError("failure_kind 须为非空字符串")
        if not allowed_statuses or any(status not in ("created", "running") for status in allowed_statuses):
            raise ValueError("allowed_statuses 只接受 created/running")
        placeholders = ",".join("?" for _ in allowed_statuses)
        with self.daemon.transaction() as conn:
            changed = conn.execute(
                "UPDATE runner_call SET status='aborted',failure_kind=?,finished_at=CURRENT_TIMESTAMP "
                f"WHERE id=? AND status IN ({placeholders})",
                (failure_kind.strip()[:200], runner_call_id, *allowed_statuses)).rowcount
            if changed != 1:
                raise RuntimeError(f"runner_call {runner_call_id} 不在可证明未启动状态 {allowed_statuses}")

    def finish_call_in_txn(self, conn: Any, *, runner_call_id: int, status: str,
                           usage: Optional[CallUsage], failure_kind: Optional[str] = None,
                           transcript_ref: Optional[str] = None,
                           execution_receipt_ref: Optional[str] = None,
                           provider_receipt_ref: Optional[str] = None) -> Optional[dict]:
        """在调用方事务内把既有 running intent、provider 回执与 ledger 原子收口。"""
        if not self.daemon.owns_active_transaction(conn):
            raise RuntimeError("finish_call_in_txn 必须运行在唯一 WriteDaemon 的 active transaction 内")
        if status not in ("success", "failed", "aborted"):
            raise ValueError(f"runner_call 终态非法: {status}")
        if status != "success" and not (isinstance(failure_kind, str) and failure_kind.strip()):
            raise ValueError(f"runner_call {status} 须给 failure_kind")
        row = conn.execute(
            "SELECT cycle_id,phase,purpose,status FROM runner_call WHERE id=?",
            (runner_call_id,)).fetchone()
        if row is None or row[3] != "running":
            raise RuntimeError(
                f"runner_call {runner_call_id} 非 running（{row[3] if row else 'missing'}），不可 finish")
        invocation = self._load_provider_invocation(
            runner_call_id=runner_call_id, row=row, usage=usage,
            execution_receipt_ref=execution_receipt_ref,
            provider_receipt_ref=provider_receipt_ref)
        changed = conn.execute(
            "UPDATE runner_call SET status=?,failure_kind=?,"
            "transcript_ref=COALESCE(?,transcript_ref),finished_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='running'",
            (status, failure_kind, transcript_ref, runner_call_id)).rowcount
        if changed != 1:
            raise RuntimeError(f"runner_call {runner_call_id} 终态迁移竞态")
        budget_hit = self.insert_ledger_for_runner(
            conn, runner_call_id=runner_call_id, usage=usage)
        if invocation is not None:
            self._record_provider_accounting(
                conn, invocation=invocation, terminal_status=status)
        return budget_hit

    def finish_call(self, *, runner_call_id: int, status: str,
                    usage: Optional[CallUsage], failure_kind: Optional[str] = None,
                    transcript_ref: Optional[str] = None,
                    execution_receipt_ref: Optional[str] = None,
                    provider_receipt_ref: Optional[str] = None) -> None:
        """自开短事务收口既有 intent；成本不可验证时复用同一 intent fail-closed。"""
        budget_hit = None
        try:
            with self.daemon.transaction() as conn:
                budget_hit = self.finish_call_in_txn(
                    conn, runner_call_id=runner_call_id, status=status, usage=usage,
                    failure_kind=failure_kind, transcript_ref=transcript_ref,
                    execution_receipt_ref=execution_receipt_ref,
                    provider_receipt_ref=provider_receipt_ref)
        except Exception as error:
            row = self.daemon.query_one(
                "SELECT cycle_id,phase,purpose,status FROM runner_call WHERE id=?",
                (runner_call_id,))
            if row is None or row[3] != "running":
                raise
            with self.daemon.transaction() as conn:
                self.fail_existing_unaccounted_call(
                    conn, runner_call_id=runner_call_id,
                    failure_kind="cost_accounting", cause=error)
                if transcript_ref is not None:
                    conn.execute(
                        "UPDATE runner_call SET transcript_ref=? WHERE id=?",
                        (transcript_ref, runner_call_id))
            raise CostAccountingFailed(
                f"cost_accounting_failed: {row[1]}/{row[2]}: {error}",
                runner_call_id=runner_call_id) from error
        if budget_hit is not None:
            raise BudgetExhausted(**budget_hit)

    def _load_provider_invocation(
            self, *, runner_call_id: int, row, usage: Optional[CallUsage],
            execution_receipt_ref: Optional[str],
            provider_receipt_ref: Optional[str]) -> Optional[ProviderInvocation]:
        """Load the durable provider fact and reject any caller/receipt usage split-brain."""
        if execution_receipt_ref is None and provider_receipt_ref is None:
            return None
        if execution_receipt_ref is None:
            raise RuntimeError("provider receipt 入账必须同时给 execution receipt ref")
        expected = provider_receipt_for_execution(execution_receipt_ref, runner_call_id)
        if provider_receipt_ref is not None and str(expected) != str(provider_receipt_ref):
            raise RuntimeError("provider receipt ref 与 execution/runner_call 确定性路径不一致")
        cycle_id, phase, purpose, _status = row
        invocation = load_provider_invocation_receipt(
            expected, expected_runner_call_id=runner_call_id,
            expected_cycle_id=(f"c{cycle_id}" if cycle_id is not None else ""),
            expected_phase=phase, expected_purpose=purpose,
            expected_execution_receipt_ref=execution_receipt_ref)
        if self._validated_usage(usage) != self._validated_usage(invocation.usage):
            raise RuntimeError(
                f"runner_call {runner_call_id} 调用方 usage 与 durable provider receipt 不一致")
        return invocation

    def _record_provider_accounting(self, conn: Any, *, invocation: ProviderInvocation,
                                    terminal_status: str) -> None:
        """Bind the exact usage receipt to the local policy billing projection once."""
        duplicate = conn.execute(
            "SELECT id FROM decision WHERE actor='orchestrator' "
            "AND type='provider_invocation_accounted' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.runner_call_id')=? ORDER BY id",
            (invocation.runner_call_id,)).fetchall()
        if duplicate:
            raise RuntimeError(
                f"runner_call {invocation.runner_call_id} 已有 provider accounting decision {duplicate}")
        ledger = conn.execute(
            "SELECT id,tokens_input,tokens_output,tokens_total,wallclock_sec,money,policy_version "
            "FROM ledger WHERE runner_call_id=?",
            (invocation.runner_call_id,)).fetchone()
        if ledger is None:
            raise RuntimeError("provider accounting 缺同事务 ledger 行")
        payload = {
            "protocol": "provider-accounting-v1",
            "runner_call_id": invocation.runner_call_id,
            "provider_receipt_ref": invocation.receipt_ref,
            "provider_receipt_sha256": invocation.receipt_sha256,
            "provider": invocation.provider,
            "model": invocation.model,
            "effort": invocation.effort,
            "local_invocation_id": invocation.local_invocation_id,
            "provider_invocation_id": invocation.provider_invocation_id,
            "provider_invocation_id_kind": invocation.provider_invocation_id_kind,
            "usage_source": invocation.usage_source,
            "execution_receipt_ref": invocation.execution_receipt_ref,
            "execution_receipt_sha256": invocation.execution_receipt_sha256,
            "execution_operation_id": invocation.execution_operation_id,
            "ledger_id": ledger[0],
            "tokens_input": ledger[1],
            "tokens_output": ledger[2],
            "tokens_total": ledger[3],
            "wallclock_sec": ledger[4],
            "money": ledger[5],
            "policy_version": ledger[6],
            "runner_terminal_status": terminal_status,
            "billing_basis": "local_policy_projection",
            "external_invoice_available": False,
        }
        conn.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (?,'orchestrator','provider_invocation_accounted',?)",
            (_cnum(invocation.cycle_id), json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))

    def record_ledger_only(self, *, runner_call_id: int, usage: Optional[CallUsage]) -> None:
        """自开短 txn：只 INSERT ledger，引用**既有** runner_call。
        cycle_id/phase **从该 runner_call 派生**（INSERT…SELECT），不信调用方复述——防交叉 cycle / phase 不一致（外审 SHOULD）。
        runner_call 不存在时抛错，禁止空写伪装成成功记账。"""
        row = self.daemon.query_one(
            "SELECT cycle_id,phase,purpose FROM runner_call WHERE id=?", (runner_call_id,))
        if row is None:
            self.fail_closed(
                cycle_id=None, phase="orchestrator", purpose=f"ledger-only:{runner_call_id}",
                cause=RuntimeError(f"runner_call {runner_call_id} 不存在，ledger 未写入"))
        budget_hit = None
        try:
            with self.daemon.transaction() as conn:
                budget_hit = self.insert_ledger_for_runner(conn, runner_call_id=runner_call_id, usage=usage)
        except Exception as e:
            self.fail_closed(cycle_id=f"c{row[0]}", phase=row[1], purpose=row[2], cause=e)
        if budget_hit is not None:
            raise BudgetExhausted(**budget_hit)

    def fail_closed(self, *, cycle_id: Optional[str], phase: str, purpose: str,
                    cause: Exception) -> None:
        """预算开启时把「成本不可信」变成 durable stop；提交后抛 typed 异常。

        原记账事务已回滚，故这里单独写一条 failed runner_call（无伪造 ledger 金额）与
        global_stop。若 DB 本身不可写，保留原异常 fail loud，不伪称已持久停机。
        """
        effective = (effective_budget_config(
            self.daemon.conn, self._base_budget, require_schedule=False)
            if self.daemon is not None else self._base_budget)
        if effective.get("session_max") is None:
            raise cause
        ci = _cnum(cycle_id) if cycle_id is not None else None
        try:
            with self.daemon.transaction() as conn:
                rc = conn.execute(
                    "INSERT INTO runner_call(cycle_id,phase,purpose,status,failure_kind) "
                    "VALUES (?,?,?,'failed','cost_accounting')", (ci, phase, purpose)).lastrowid
                payload = {"reason": "cost_accounting_failed", "phase": phase, "purpose": purpose,
                           "runner_call_id": rc, "error_type": type(cause).__name__,
                           "error": str(cause)[:500]}
                if conn.execute(
                        "SELECT 1 FROM decision WHERE actor='orchestrator' AND type='global_stop' LIMIT 1"
                ).fetchone() is None:
                    conn.execute(
                        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                        "VALUES (?,'orchestrator','global_stop',?)",
                        (ci, json.dumps(payload, ensure_ascii=False)))
        except Exception:
            raise cause
        raise CostAccountingFailed(
            f"cost_accounting_failed: {phase}/{purpose}: {cause}", runner_call_id=rc) from cause

    def insert_ledger_for_runner(self, conn: Any, *, runner_call_id: int,
                                 usage: Optional[CallUsage]) -> Optional[dict]:
        """在调用方已有事务内补 ledger；JudgeProvider 用它把 runner_call+ledger+DECISION 原子提交。

        cycle_id/phase 从 runner_call 派生，且 runner_call 不存在时 fail loud，避免 INSERT…SELECT 空写后
        调用方误以为已经记账。此方法不开事务，必须传 WriteDaemon.transaction() 给出的连接。
        """
        if not self.daemon.owns_active_transaction(conn):
            raise RuntimeError("ledger INSERT 必须运行在唯一 WriteDaemon 的 active transaction 内")
        # WriteDaemon owns the sole writer connection and holds one RLock over
        # BEGIN IMMEDIATE..COMMIT.  Consequently this precheck+INSERT and the
        # provider-accounting decision are one serialized critical section;
        # the frozen Appendix-A schema need not be mutated with a new index.
        if conn.execute("SELECT 1 FROM ledger WHERE runner_call_id=? LIMIT 1",
                        (runner_call_id,)).fetchone() is not None:
            raise RuntimeError(f"runner_call {runner_call_id} 已有 ledger，拒绝重复记账")
        effective = effective_budget_config(conn, self._base_budget, require_schedule=False)
        budget_enabled = effective["session_max"] is not None
        u = self._validated_usage(usage)
        money = self.money_for(u, budget_enabled=budget_enabled)
        # Preserve the historical base-policy fingerprint byte-for-byte until a
        # live override actually changes the projection; numeric normalization
        # alone (100000 -> 100000.0) must not fork ledger identity.
        policy_version = (self.policy_version
                          if effective == validate_budget_config(
                              self._base_budget, require_schedule=False)
                          else policy_fingerprint(policy_with_effective_budget(self.policy, effective)))
        cur = conn.execute(
            "INSERT INTO ledger(cycle_id,phase,runner_call_id,tokens_input,tokens_output,tokens_total,"
            "wallclock_sec,money,policy_version) "
            "SELECT cycle_id, phase, id, ?, ?, ?, ?, ?, ? FROM runner_call WHERE id=?",
            (u.tokens_input, u.tokens_output, u.tokens_total, u.wallclock_sec,
             money, policy_version, runner_call_id))
        if cur.rowcount != 1:
            raise RuntimeError(f"runner_call {runner_call_id} 不存在，ledger 未写入")
        return self._record_budget_stop_if_needed(conn, effective)

    def fail_existing_unaccounted_call(self, conn: Any, *, runner_call_id: int,
                                       failure_kind: str, cause: Exception,
                                       terminal_status: str = "failed") -> Optional[dict]:
        """把已有调用意图终态化为“成本未知”；调用方须在同一事务内补其业务失败回执。

        interaction_query 在外部调用**之前**先落 ``created``，主线程提交 ``running`` 后才放行 worker；
        若恢复时只剩 running intent，无法知道调用发生到哪一步，也没有可信 usage 可写 ledger：预算开启时
        必须沿用 CostLedger 的 fail-closed 原则落 durable global_stop；预算显式关闭时只记失败。
        本方法不开事务、不另造第二条 runner_call，保持原 intent 的审计身份。
        """
        if not isinstance(failure_kind, str) or not failure_kind.strip():
            raise ValueError("failure_kind 须为非空字符串")
        if terminal_status not in ("failed", "aborted"):
            raise ValueError("未知成本调用只可收口为 failed/aborted")
        row = conn.execute(
            "SELECT cycle_id,phase,purpose,status FROM runner_call WHERE id=?",
            (runner_call_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"runner_call {runner_call_id} 不存在，无法终态化未知成本")
        cycle_id, phase, purpose, status = row
        if status not in ("created", "running"):
            raise RuntimeError(
                f"runner_call {runner_call_id} 已是终态 {status}，拒绝改写为未知成本失败")
        changed = conn.execute(
            "UPDATE runner_call SET status=?,failure_kind=?,finished_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status IN ('created','running')",
            (terminal_status, failure_kind.strip()[:200], runner_call_id)).rowcount
        if changed != 1:
            raise RuntimeError(f"runner_call {runner_call_id} 未知成本终态迁移竞态")

        effective = effective_budget_config(conn, self._base_budget, require_schedule=False)
        if effective["session_max"] is None:
            return None
        payload = {
            "reason": "cost_accounting_failed",
            "phase": phase,
            "purpose": purpose,
            "runner_call_id": runner_call_id,
            "error_type": type(cause).__name__,
            "error": str(cause)[:500],
        }
        if conn.execute(
                "SELECT 1 FROM decision WHERE actor='orchestrator' AND type='global_stop' LIMIT 1"
        ).fetchone() is None:
            conn.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (?,'orchestrator','global_stop',?)",
                (cycle_id, json.dumps(payload, ensure_ascii=False)))
        return payload

    def _record_budget_stop_if_needed(self, conn: Any, effective_budget: Optional[dict] = None) -> Optional[dict]:
        """在 ledger 写事务内检查累计并幂等落 global_stop；只返回命中，绝不在事务内抛。"""
        budget = effective_budget or effective_budget_config(
            conn, self._base_budget, require_schedule=False)
        session_max = budget["session_max"]
        if session_max is None:
            return None
        spent = conn.execute("SELECT COALESCE(SUM(money),0) FROM ledger").fetchone()[0]
        if spent < session_max:
            return None
        hit = {"spent": spent, "session_max": session_max}
        if conn.execute("SELECT 1 FROM decision WHERE actor='orchestrator' AND type='global_stop' "
                        "LIMIT 1").fetchone() is None:
            conn.execute(
                "INSERT INTO decision(actor,type,payload_json) VALUES ('orchestrator','global_stop',?)",
                (json.dumps({"reason": "budget_exhausted", **hit}, ensure_ascii=False),))
        return hit
