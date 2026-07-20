"""ConsoleInboxIngest —— 人类控制台入站桥（步⑨ CP9.3）。

console_server（独立只读进程）把运维在控制台敲的命令 append 到 `<work>/state/console_inbox.jsonl`
（连接器缓冲、**非权威**）。本模块在 run 进程的 **precheck 边界**把这些行 ingest 进权威入站链：

  未消费行 → Console.handle_inbound（幂等：写 interaction_message + 分类落 directive/note）
           → intent=query 再交给 Mediator（模板同步回复；Codex 先写 durable runner intent、后台只读应答）
  action 行   → 先写 interaction_message + unclear 分类（显式控件动作，不重跑自然语言分类）
               → Console.confirm_directive / reject_directive（确认绑定 message provenance）
  file_request action 行 → 按 request 所属 goal 写 interaction_message + unclear 分类
               → FileRequestService.resolve/cancel（单写者复制/hash/终态迁移）

**幂等与游标**：correctness 由持久层保证，游标 `console_inbox.cursor`（已消费 committed record 的
**byte offset + inbox identity/anchor**）是纯优化、
可丢：
- directive/note：interaction_message UNIQUE(connector, idempotency_key) → 重复 ingest 命中既有 message、不重复建。
- query：模板路径以 reply 存在性去重；Codex 路径以 ``phase=interaction_query,purpose=message:<id>`` 的
  durable runner intent 接受后立即推进，不等待外部调用。游标丢失/崩溃重放会命中同一 reply/intent；未知在途
  调用只终态化、不重发，故研究推进不被慢 query 阻塞，也不会制造重复外部调用。
- directive action：动作 message 复用同一 idempotency key；confirmed/rejected 已达成即 no-op。因而
  「message 已落库、directive 尚未更新」的 crash window 可重放，游标丢失也不会重复改写 provenance。
- file_request action：终态的 resolved_message_id 等于本动作 message 才视为 replay no-op；其他终态/目标
  不存在均清楚拒绝。resolve 的虚拟 source_ref 在消费时重新做 input/work uploads containment 校验。
- 游标按 committed LF 边界增量读取：坏 JSON / 缺序号/超长的已提交行也能被「跳过并推进」，不会每拍全量重读；
  inode + 前缀 anchor 不符就从头按 DB 幂等重放。torn-tail 未终止行不可见，下轮补齐再消费。

**健壮性（入站故障不让进程裸崩，也不能让研究越过尚未消费的控制动作）**：
- **顶层兜底** try/except：读 spool / 写游标的 I/O 故障记 warning，并令 ``has_pending`` 保持真；
  precheck 因而停在安全边界，由常驻 run 下轮重试，而不是继续调用研究 provider。
- **有限重试**：瞬时故障（DB locked/busy、卡片尚未发布 FileNotFoundError 等）→ 停批、不推进游标，下轮 precheck 重试；
  连续 `_MAX_ATTEMPTS` 次仍败 → 判**持久故障**、按终态处理并推进（防单条卡死饿死整队）：
    · handle_inbound 持久败 → 原子落 `unclear` 分类 + 可见失败回执，二者 durable 后才推进；
    · query 应答持久败 → 写一条**终态失败回执**；生产 Codex 路径另绑 aborted interaction_query runner_call
      （reply 存在→守卫防再答），推进——既不漏答、不双答、也不饿死。
- **毒消息**只限无法形成安全幂等语义的外部形状错误（坏 JSON、坏 identity 等）；通过形状闸后的
  handle_inbound 任意内部异常都走上述持久重试/终态回执，不能静默丢弃。

**并发/单写**：console_server 只 append spool + mode=ro 读 DB；run 进程内 resident pump 与
precheck 经同一 ingest lock 独占 cursor/retry sidecar，DB 操作再经 WriteDaemon 唯一连接串行化。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from .console_spool import ConsoleSpool, SpoolBatch, open_pinned_upload_ref
from .ids import parse_positive_sqlite_int
from .console import (CONSOLE_MESSAGE_SESSION_REF, DIRECTIVE_ACTION_SESSION_REF,
                      FILE_REQUEST_ACTION_SESSION_REF, NARRATOR_QUERY_SESSION_REF,
                      IdempotencyCollisionError, directive_action_text)

logger = logging.getLogger(__name__)
_MAX_REASON_CHARS = 2_000
_MAX_RAW_TEXT_CHARS = 20_000
_MAX_IDEMPOTENCY_KEY_CHARS = 256
_ACTION_FAILURE_PREFIX = "[console-action-failed] "
_INBOUND_FAILURE_PREFIX = "[console-inbound-failed] "
_CONVERSATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class RetryStateError(RuntimeError):
    """Retry sidecar could not be durably changed; the current spool record must stay pending."""


def _action_positive_id(value: Any, *, label: str) -> int:
    """spool 是外部输入面：拒绝 bool/float/越界，且在 ``int`` 前做 SQLite 边界检查。"""
    if isinstance(value, bool):
        raise ValueError(f"{label} 须为正整数")
    if isinstance(value, int):
        digits = str(value)
    elif isinstance(value, str):
        digits = value.strip()
    else:
        raise ValueError(f"{label} 须为正整数")
    parsed = parse_positive_sqlite_int(digits, label=label)
    if digits != str(parsed):
        raise ValueError(f"{label} 须为规范正整数")
    return parsed


class ConsoleInboxIngest:
    """把 console_inbox.jsonl 未消费行 ingest 进 Console/Mediator（precheck 边界调用；见模块 docstring）。"""

    _MAX_ATTEMPTS = 5                          # 瞬时故障自愈窗口；超限=持久故障 → 终态处理并推进（防饥饿）

    def __init__(self, console, mediator, work_root: str, *, file_requests=None,
                 system_root: Optional[str] = None, spool: Optional[ConsoleSpool] = None,
                 source_label: str = "console_inbox"):
        self.console = console
        self.mediator = mediator
        self.work_root = Path(work_root)
        self.system_root = Path(system_root) if system_root is not None else None
        self.file_requests = file_requests
        state = self.work_root / "state"
        self.spool = spool or ConsoleSpool(self.work_root)
        self.source_label = source_label
        self.inbox = self.spool.inbox_path
        self.cursor_path = state / "console_inbox.cursor"
        self.retry_path = state / ".console_inbox.retry.json"
        # idempotency_key → 已失败次数。sidecar 原子+fsync 持久化；因此正常 CLI 在 pending 阻断时退出/重启，
        # 也不会把队首动作的有限重试预算清零并永久饿死后续 cancel。
        self._attempts: dict[str, int] = {}
        self._retry_state_error: Optional[Exception] = None
        self._retry_write_error: Optional[Exception] = None
        # Research precheck and the resident interaction pump may both probe
        # the spool.  One lock owns cursor/retry-sidecar transitions and also
        # serializes Mediator's in-memory inflight map.
        self._ingest_lock = threading.RLock()
        self._reload_retry_state()
        self.has_pending = self._retry_state_error is not None

    def _reload_retry_state(self) -> bool:
        """读取受 stable claim 保护的计数；损坏时 fail-closed，绝不重置预算后继续消费。"""
        try:
            self._attempts = self.spool.load_retry_counts()
            self._retry_state_error = None
            return True
        except Exception as error:              # 路径/内容/IO 任一不可信：不 ingest、不覆盖证据
            self._retry_state_error = error
            logger.error("console_inbox retry state 不可安全读取 → 本轮 fail-closed、不消费", exc_info=True)
            return False

    def _persist_attempts(self) -> None:
        """原子+fsync 持久化；失败向上抛，使当前 spool record 不推进。"""
        try:
            self.spool.store_retry_counts(self._attempts)
            self._retry_write_error = None
        except Exception as error:
            self._retry_write_error = error
            raise RetryStateError("console_inbox retry state 持久化失败") from error

    def _clear_attempt(self, idem: str) -> None:
        if idem in self._attempts:
            previous = self._attempts.pop(idem)
            try:
                self._persist_attempts()
            except Exception:
                self._attempts[idem] = previous
                raise

    def ingest(self, cyc: Any = None) -> int:
        with self._ingest_lock:
            return self._ingest_locked(cyc)

    def poll_accepted(self) -> None:
        """Finalize only already durable query work; do not consume newly appended spool records."""
        with self._ingest_lock:
            self.mediator.poll()

    @property
    def interaction_pending(self) -> bool:
        with self._ingest_lock:
            return self.has_pending or self.mediator.has_pending_queries

    @property
    def accepted_interaction_pending(self) -> bool:
        with self._ingest_lock:
            return self.mediator.has_pending_queries

    def _ingest_locked(self, cyc: Any = None) -> int:
        """消费 cursor 之后所有已提交行；返回本次处理条数。cyc=当前 cycle（有则把 directive 绑到该轮）。
        **顶层兜底**：读 spool / 写游标故障不抛崩主循环，但置 ``has_pending`` 阻断研究推进，下轮重试。"""
        try:
            # Codex worker 永不持 DB；只有本 precheck/main-writer 边界可以把完成回执原子写入
            # runner_call + ledger + interaction_reply。poll 不等待，因此不会把研究链卡在模型调用上。
            self.mediator.poll()
            if self._retry_state_error is not None and not self._reload_retry_state():
                self.has_pending = True
                return 0
            if self._retry_write_error is not None:
                # Do not redispatch a DB action while its retry transition was
                # not durable.  Probe the exact rolled-back state; after
                # recovery wait one more poll before touching the action.
                try:
                    self.spool.store_retry_counts(self._attempts)
                except Exception as error:
                    self._retry_write_error = error
                    self.has_pending = True
                    return 0
                self._retry_write_error = None
                self.has_pending = True
                return 0
            return self._ingest(cyc)
        except Exception:                      # noqa: BLE001 —— read_text/_set_cursor 等边界 I/O 故障兜底（例：磁盘满、路径被替换）
            self.has_pending = True
            logger.warning("console_inbox ingest 顶层异常，跳过本轮", exc_info=True)
            return 0

    def _ingest(self, cyc: Any) -> int:
        batch = self.spool.read_pending()
        cycle_id: Optional[str] = getattr(cyc, "cycle_id", None)
        processed = 0
        consumed = batch.start_offset
        retry_pending = False
        for record in batch.records:
            if record.line is None:
                logger.warning("console_inbox 超限记录跳过: %s", record.error)
                consumed = record.end_offset
                continue
            line = record.line.strip()
            if not line:
                consumed = record.end_offset   # 空行照消费（推进 byte offset）
                continue
            outcome = self._dispatch(line, cycle_id)
            if outcome == "retry":
                retry_pending = True
                break                          # 瞬时故障：停在此行前、不推进游标越过它，下轮 precheck 重试（不丢消息）
            consumed = record.end_offset       # ok / poison → committed LF 边界照进
            if outcome == "ok":
                processed += 1
        if consumed > batch.start_offset:
            self._set_cursor(batch, consumed)
        self.has_pending = retry_pending or batch.has_more_committed
        return processed

    def _set_cursor(self, batch: SpoolBatch, consumed_offset: int) -> None:
        self.spool.write_cursor(batch, consumed_offset)

    def _dispatch(self, line: str, cycle_id: Optional[str]) -> str:
        """解析 + 处理单行，返回 'ok'|'poison'|'retry'。坏 JSON / 非对象 → poison（跳过并推进）。"""
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("console_inbox 坏 JSON 跳过: %r", line[:120])
            return "poison"
        if not isinstance(rec, dict):          # 合法 JSON 但非 object（"x" / [] / 3）→ 无法作记录 → 跳过并推进（否则 rec.get 抛、每拍重扫）
            logger.warning("console_inbox 非对象记录跳过: %r", line[:120])
            return "poison"
        return self._process_one(rec, cycle_id)

    def _process_one(self, rec: dict, cycle_id: Optional[str]) -> str:
        """单条入站：handle_inbound（幂等）→ query 转 _answer_query。返回 'ok'|'poison'|'retry'。"""
        raw = rec.get("raw_text", "")
        seq = rec.get("seq")
        idem = rec.get("idempotency_key") or (f"console-{seq}" if seq is not None else None)
        connector = rec.get("connector", "console")
        conversation_id = rec.get("conversation_id")
        if (not isinstance(idem, str) or not idem
                or len(idem) > _MAX_IDEMPOTENCY_KEY_CHARS):
            # 无规范幂等键且无 seq → 无法安全去重 → 判毒（跳过并推进），console_server 恒带二者
            logger.warning("console_inbox idempotency_key 缺失或过长，跳过: %r", str(rec)[:120])
            return "poison"
        if connector != "console":
            logger.warning("console_inbox connector 非固定 console，跳过: %r", connector)
            self._clear_attempt(idem)
            return "poison"
        action_target = rec.get("action_target")
        if action_target == "file_request":
            return self._process_file_request_action(rec, connector=connector, idem=idem)
        if action_target not in (None, "query"):
            logger.warning("console_inbox action_target 非法，跳过: %r", action_target)
            self._clear_attempt(idem)
            return "poison"
        if "action" in rec:
            return self._process_directive_action(rec, connector=connector, idem=idem, cycle_id=cycle_id)
        if (conversation_id is not None
                and (not isinstance(conversation_id, str)
                     or _CONVERSATION_ID_RE.fullmatch(conversation_id) is None)):
            logger.warning("console_inbox conversation_id 非 128-bit hex，跳过: idem=%s", idem)
            self._clear_attempt(idem)
            return "poison"
        if not isinstance(raw, str) or not raw.strip() or len(raw) > _MAX_RAW_TEXT_CHARS:
            logger.warning("console_inbox 普通消息 raw_text 非法，跳过: idem=%s", idem)
            self._clear_attempt(idem)
            return "poison"
        forced_query = action_target == "query"
        return self._process_text(
            connector=connector, raw=raw, idem=idem, cycle_id=cycle_id,
            conversation_id=conversation_id,
            session_ref=(NARRATOR_QUERY_SESSION_REF if forced_query
                         else CONSOLE_MESSAGE_SESSION_REF),
            force_query=forced_query)

    def _process_text(self, *, connector: str, raw: str, idem: str,
                      cycle_id: Optional[str], conversation_id: Optional[str],
                      session_ref: str, strict_failures: bool = False,
                      force_query: bool = False) -> str:
        """Process one already authenticated/shape-checked natural-language record."""
        if self._attempts.get(idem, 0) >= self._MAX_ATTEMPTS:
            if strict_failures:
                raise RuntimeError(
                    f"{self.source_label} DB ingest 重试达到上限，拒绝伪装成 unclear")
            goal_id, goal_ver = self._message_goal_binding(connector, idem)
            return self._terminalize_inbound_failure(
                connector=connector, raw=raw, idem=idem, cycle_id=cycle_id,
                goal_id=goal_id, goal_ver=goal_ver, conversation_id=conversation_id,
                session_ref=session_ref)
        goal_id: Optional[int] = None
        goal_ver: Optional[int] = None
        try:
            goal_id, goal_ver = self._message_goal_binding(connector, idem)
            handler = (self.console.handle_query_inbound
                       if force_query else self.console.handle_inbound)
            res = handler(connector=connector, raw_text=raw,
                          idempotency_key=idem, cycle_id=cycle_id,
                          goal_id=goal_id, goal_ver=goal_ver,
                          session_ref=session_ref,
                          conversation_id=conversation_id)
            self._verify_inbound_message(
                res["message_id"], raw=raw, idem=idem,
                goal_id=goal_id, goal_ver=goal_ver,
                conversation_id=conversation_id, connector=connector,
                session_ref=session_ref)
        except IdempotencyCollisionError:
            logger.error("console_inbox 普通消息 idempotency collision (idem=%s) → 拒绝", idem, exc_info=True)
            if strict_failures:
                raise
            self._clear_attempt(idem)
            return "poison"
        except sqlite3.OperationalError:
            attempts = self._bump(idem)
            logger.log(logging.ERROR if attempts >= self._MAX_ATTEMPTS else logging.WARNING,
                       "console_inbox ingest 内部故障 (idem=%s, 第%d次)", idem, attempts, exc_info=True)
            if attempts < self._MAX_ATTEMPTS:
                return "retry"
            if strict_failures:
                raise RuntimeError(
                    f"{self.source_label} DB ingest 重试达到上限，保持 durable cursor")
            if goal_id is None or goal_ver is None:
                goal_id, goal_ver = self._message_goal_binding(connector, idem)
            return self._terminalize_inbound_failure(
                connector=connector, raw=raw, idem=idem, cycle_id=cycle_id,
                goal_id=goal_id, goal_ver=goal_ver, conversation_id=conversation_id,
                session_ref=session_ref)
        except Exception:                      # noqa: BLE001 —— console 辅助面保留既有有限终态；认证 connector fail-loud
            if strict_failures:
                raise
            attempts = self._bump(idem)
            logger.log(logging.ERROR if attempts >= self._MAX_ATTEMPTS else logging.WARNING,
                       "console_inbox ingest 内部故障 (idem=%s, 第%d次)", idem, attempts, exc_info=True)
            if attempts < self._MAX_ATTEMPTS:
                return "retry"
            if goal_id is None or goal_ver is None:
                goal_id, goal_ver = self._message_goal_binding(connector, idem)
            return self._terminalize_inbound_failure(
                connector=connector, raw=raw, idem=idem, cycle_id=cycle_id,
                goal_id=goal_id, goal_ver=goal_ver, conversation_id=conversation_id,
                session_ref=session_ref)
        if res.get("intent") == "query" and res.get("message_id") is not None:
            return self._answer_query(
                idem, res["message_id"], special=res.get("special"))
        self._clear_attempt(idem)              # 成功 → 清计数
        return "ok"

    def _message_goal_binding(self, connector: str, idem: str) -> tuple[Optional[int], Optional[int]]:
        """Bind a spool message to one immutable goal version across retries.

        If the durable message already exists, its original binding wins.  On
        first ingest the current latest goal is captured.  This is required for
        stale goal-amend detection and prevents a cursor retry after a revision
        from colliding with its own previously stored message.
        """
        existing = self.console.daemon.query_one(
            "SELECT goal_id,goal_ver FROM interaction_message "
            "WHERE connector=? AND idempotency_key=?", (connector, idem))
        if existing is not None:
            # Pre-CP11.2b.3 messages were intentionally stored with no goal
            # binding.  Preserve that immutable legacy tuple on replay so a
            # crash between message/classification can still converge; only
            # newly ingested records receive the current version binding.
            return (int(existing[0]), int(existing[1])) if existing[0] is not None else (None, None)
        current = self.console.daemon.query_one(
            "SELECT id,version FROM goal ORDER BY version DESC LIMIT 1")
        if current is None:
            raise RuntimeError("控制台普通消息入站时当前 goal 不存在")
        return int(current[0]), int(current[1])

    def _verify_inbound_message(self, mid: int, *, raw: str, idem: str,
                                goal_id: Optional[int], goal_ver: Optional[int],
                                conversation_id: Optional[str], connector: str = "console",
                                session_ref: str = CONSOLE_MESSAGE_SESSION_REF) -> None:
        row = self.console.daemon.query_one(
            "SELECT connector,raw_text,raw_hash,goal_id,goal_ver,session_ref,conversation_id "
            "FROM interaction_message WHERE id=?", (mid,))
        expected_hash = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if row != (connector, raw, expected_hash, goal_id, goal_ver,
                   session_ref, conversation_id):
            raise IdempotencyCollisionError(
                f"{connector} idempotency_key 已绑定其他普通消息: {idem}")

    def _terminalize_inbound_failure(self, *, connector: str, raw: str, idem: str,
                                     cycle_id: Optional[str], goal_id: Optional[int],
                                     goal_ver: Optional[int], conversation_id: Optional[str],
                                     session_ref: str = CONSOLE_MESSAGE_SESSION_REF) -> str:
        """连续内部失败达上限后，先落可审计终态，再允许 cursor 越过当前记录。

        新消息的 ``unclear`` 分类和失败 reply 共用一个事务；事务/commit 任一失败都返回 ``retry``。
        retry sidecar 只在该事务成功后清理，因此崩溃重放要么继续补终态，要么看到既有终态收敛，
        不会出现「游标已推进但没有权威分类/回执」的窗口。
        """
        try:
            mid = self.console.ingest.inbound(
                connector=connector, raw_text=raw, idempotency_key=idem, cycle_id=cycle_id,
                goal_id=goal_id, goal_ver=goal_ver,
                session_ref=session_ref,
                conversation_id=conversation_id)
            self._verify_inbound_message(
                mid, raw=raw, idem=idem, goal_id=goal_id, goal_ver=goal_ver,
                conversation_id=conversation_id, connector=connector,
                session_ref=session_ref)
            failure_text = _INBOUND_FAILURE_PREFIX + "控制台消息处理持续失败，未执行任何控制语义"
            failure_hash = "sha256:" + hashlib.sha256(failure_text.encode("utf-8")).hexdigest()
            with self.console.daemon.transaction() as conn:
                classification = conn.execute(
                    "SELECT intent,directive_id FROM interaction_classification WHERE message_id=?", (mid,)
                ).fetchone()
                if classification is None:
                    conn.execute(
                        "INSERT INTO interaction_classification(message_id,intent,directive_id) "
                        "VALUES (?,'unclear',NULL)", (mid,))
                    classification = ("unclear", None)
                if classification[0] == "unclear":
                    exists = conn.execute(
                        "SELECT 1 FROM interaction_reply WHERE message_id=? LIMIT 1", (mid,)).fetchone()
                    if exists is None:
                        conn.execute(
                            "INSERT INTO interaction_reply(message_id,reply_ref,reply_hash,reply_text,"
                            "snapshot_cycle,responder_kind) VALUES (?,?,?,?,NULL,'template')",
                            (mid, f"reply:{mid}", failure_hash, failure_text))
            if classification[0] == "query":
                # A connector ACK is not a final query receipt.  Preserve any
                # active intent, or create an auditable no-call terminal if
                # preparation itself remains broken.
                if self.mediator.async_enabled:
                    try:
                        self.mediator.enqueue_query(message_id=mid)
                    except Exception as cause:
                        self.mediator.terminalize_query_prepare_failure(
                            message_id=mid, cause=cause)
                else:
                    self.mediator.handle_query(message_id=mid)
            self._clear_attempt(idem)
            return "ok" if classification[0] in ("directive", "note", "query") else "poison"
        except RetryStateError:
            raise
        except IdempotencyCollisionError:
            # A reused key bound to different immutable raw is a client
            # protocol violation, not an infrastructure retry.  The rejected
            # line remains in the append-only spool for audit.
            logger.error("console_inbox 普通消息 idempotency collision (idem=%s) → 拒绝", idem, exc_info=True)
            self._clear_attempt(idem)
            return "poison"
        except Exception:                      # terminal receipt itself must be durable before cursor advance
            logger.error("console_inbox 普通消息终态回执失败 (idem=%s) → 保持阻断", idem, exc_info=True)
            return "retry"

    def _process_directive_action(self, rec: dict, *, connector: str, idem: str,
                                  cycle_id: Optional[str]) -> str:
        """处理显式控件动作：先落 durable message，再改 directive。

        这里不用 ``handle_inbound`` 重新猜意图：action 已由按钮结构化表达，若把拒绝理由送入
        关键词分类器，理由中的「暂停/预算」反而可能创建新 directive。受冻结 DDL 约束，控件
        动作记为 ``unclear`` 分类；其权威语义由 spool 的 action + directive_id 和后续状态转移表达。
        """
        action = rec.get("action")
        did = rec.get("directive_id")
        conversation_id = rec.get("conversation_id")
        if action not in ("confirm", "reject"):
            logger.warning("console_inbox directive action 形状非法，跳过: %r", str(rec)[:160])
            self._clear_attempt(idem)
            return "poison"
        try:
            did = _action_positive_id(did, label="directive_id")
        except ValueError:
            logger.warning("console_inbox directive_id 非法，跳过: %r", did)
            self._clear_attempt(idem)
            return "poison"
        if (conversation_id is not None
                and (not isinstance(conversation_id, str)
                     or _CONVERSATION_ID_RE.fullmatch(conversation_id) is None)):
            logger.warning("console_inbox directive action conversation_id 非法: idem=%s", idem)
            self._clear_attempt(idem)
            return "poison"
        reason = ""
        if action == "reject":
            reason_value = rec.get("reason") or "用户从控制台拒绝"
            if not isinstance(reason_value, str):
                logger.warning("console_inbox directive reject reason 非字符串，跳过: idem=%s", idem)
                self._clear_attempt(idem)
                return "poison"
            reason = reason_value.strip()
            if len(reason) > _MAX_REASON_CHARS:
                logger.warning("console_inbox directive reject reason 过长，跳过: idem=%s", idem)
                self._clear_attempt(idem)
                return "poison"
        # raw_text 绑定完整结构化动作语义；reject reason 只写 hash，既防撞键也不把自由文本混入分类。
        raw = directive_action_text(action, did, reason=reason)
        mid: Optional[int] = None
        try:
            source_goal = self.console.daemon.query_one(
                "SELECT m.goal_id,m.goal_ver FROM directive d "
                "JOIN interaction_message m ON m.id=d.source_interaction_message_id WHERE d.id=?", (did,))
            mid = self.console.ingest.inbound(connector=connector, raw_text=raw,
                                              idempotency_key=idem, cycle_id=cycle_id,
                                              goal_id=(source_goal[0] if source_goal else None),
                                              goal_ver=(source_goal[1] if source_goal else None),
                                              session_ref=DIRECTIVE_ACTION_SESSION_REF,
                                              conversation_id=conversation_id)
            stored = self.console.daemon.query_one(
                "SELECT raw_text,goal_id,goal_ver,session_ref,connector,conversation_id "
                "FROM interaction_message WHERE id=?", (mid,))
            expected_stored = ((raw, source_goal[0], source_goal[1], DIRECTIVE_ACTION_SESSION_REF,
                                connector, conversation_id)
                               if source_goal else
                               (raw, None, None, DIRECTIVE_ACTION_SESSION_REF,
                                connector, conversation_id))
            if stored != expected_stored:
                raise IdempotencyCollisionError(f"action idempotency_key 已绑定其他 raw: {idem}")
            self._ensure_action_classification(mid)
            if self._has_action_failure(mid):
                self._clear_attempt(idem)
                return "poison"               # 失败回执是该 action 的 durable 终态，cursor crash 后不得复活执行
            row = self.console.daemon.query_one(
                "SELECT status, hardness, payload_json FROM directive WHERE id=?", (did,))
            if row is None:
                raise ValueError(f"directive 不存在: {did}")
            status, hardness, payload_raw = row
            payload = json.loads(payload_raw)
            if not isinstance(payload, dict):
                raise ValueError(f"directive {did} payload 不是 JSON object")
            if action == "confirm":
                # 只有**同一 provenance message** 才是重放；后来另一条 confirm 不能冒充首个动作成功。
                if payload.get("confirmed") is True:
                    if payload.get("confirmation_message_id") == mid:
                        self._clear_attempt(idem)
                        return "ok"
                    raise ValueError(f"directive {did} 已由另一条消息确认")
                if payload.get("confirmed") is not False or hardness != "hard":
                    raise ValueError(f"directive {did} 不是可确认的未确认硬指令")
                if status != "pending":
                    raise ValueError(f"directive {did} 非 pending（{status}），不可确认")
                if self._attempts.get(idem, 0) >= self._MAX_ATTEMPTS:
                    if self._record_action_failure(mid, "directive action 持久基础设施故障，未执行"):
                        self._clear_attempt(idem)
                        return "poison"
                    return "retry"
                self.console.confirm_directive(directive_id=did, confirm_message_id=mid)
            else:
                if status == "rejected":      # 也只接受同 provenance 的游标丢失重放
                    if payload.get("rejection_message_id") == mid:
                        self._clear_attempt(idem)
                        return "ok"
                    raise ValueError(f"directive {did} 已由另一条消息拒绝")
                if status != "pending":
                    raise ValueError(f"directive {did} 非 pending（{status}），不可拒绝")
                if self._attempts.get(idem, 0) >= self._MAX_ATTEMPTS:
                    if self._record_action_failure(mid, "directive action 持久基础设施故障，未执行"):
                        self._clear_attempt(idem)
                        return "poison"
                    return "retry"
                self.console.reject_directive(directive_id=did, reason=reason, reject_message_id=mid)
        except IdempotencyCollisionError:
            logger.error("console_inbox directive action idempotency collision (idem=%s) → 拒绝", idem,
                         exc_info=True)
            self._clear_attempt(idem)
            return "poison"                  # 不得把失败 reply 写到被撞键的旧权威 message
        except RetryStateError:
            raise                             # DB action 可能已成功；只保留 cursor 重放，绝不伪造业务失败回执
        except sqlite3.OperationalError:
            attempts = self._bump(idem)
            logger.log(logging.ERROR if attempts >= self._MAX_ATTEMPTS else logging.WARNING,
                       "console_inbox directive action 可重试故障 (idem=%s, 第%d次)；不丢动作、不推进游标",
                       idem, attempts, exc_info=True)
            if attempts >= self._MAX_ATTEMPTS and mid is not None and self._record_action_failure(
                    mid, "directive action 持久基础设施故障，未执行"):
                self._clear_attempt(idem)
                return "poison"
            return "retry"
        except Exception as e:                # noqa: BLE001 —— 无目标/非 pending/坏 payload 皆为终态坏动作
            logger.warning("console_inbox directive action 失败跳过 (idem=%s, action=%s, d%s)",
                           idem, action, did, exc_info=True)
            if mid is not None and not self._record_action_failure(mid, f"directive action 被拒：{e}"):
                return "retry"                # HTTP 已 ACK；失败回执也必须 durable 后才能推进
            self._clear_attempt(idem)
            return "poison"
        self._clear_attempt(idem)
        return "ok"

    def _ensure_action_classification(self, mid: int) -> None:
        """显式 action message 也满足「每消息恰一分类」；重放时不重复插入。"""
        with self.console.daemon.transaction() as conn:
            row = conn.execute(
                "SELECT intent, directive_id FROM interaction_classification WHERE message_id=?", (mid,)
            ).fetchone()
            if row is None:
                conn.execute("INSERT INTO interaction_classification(message_id,intent,directive_id) "
                             "VALUES (?,'unclear',NULL)", (mid,))
            elif row != ("unclear", None):
                # 只有人工编辑 spool/idempotency 撞键才会到此；不可把旧消息当动作 provenance。
                raise IdempotencyCollisionError(
                    f"action idempotency_key 已绑定其他分类: message {mid}")

    def _process_file_request_action(self, rec: dict, *, connector: str, idem: str) -> str:
        """处理文件请求控件动作；只有 run 进程会走到这里，故权威迁移保持单写者纪律。"""
        action, rid = rec.get("action"), rec.get("request_id")
        if action not in ("resolve", "approve", "cancel"):
            logger.warning("console_inbox file_request action 形状非法，跳过: %r", str(rec)[:200])
            self._clear_attempt(idem)
            return "poison"
        try:
            rid = _action_positive_id(rid, label="request_id")
        except ValueError:
            logger.warning("console_inbox request_id 非法，跳过: %r", rid)
            self._clear_attempt(idem)
            return "poison"

        source_ref = rec.get("source_ref")
        reason_value = rec.get("reason") or "用户从控制台取消文件请求"
        if not isinstance(reason_value, str):
            logger.warning("console_inbox file_request reason 非字符串，跳过: idem=%s", idem)
            self._clear_attempt(idem)
            return "poison"
        reason = reason_value.strip()
        if len(reason) > _MAX_REASON_CHARS:
            logger.warning("console_inbox file_request 取消理由过长，跳过: idem=%s", idem)
            self._clear_attempt(idem)
            return "poison"
        if action == "resolve":
            raw = f"解决文件请求 r{rid}，来源 {source_ref}"
        elif action == "approve":
            raw = f"同意权限请求 r{rid}"
        else:
            raw = f"取消文件请求 r{rid}：{reason}"
        mid: Optional[int] = None
        try:
            row = self.console.daemon.query_one(
                "SELECT status,goal_id,goal_ver,resolved_message_id FROM interaction_request WHERE id=?", (rid,))
            if row is None:
                raise ValueError(f"interaction_request 不存在: {rid}")
            status, goal_id, goal_ver, resolved_mid = row
            # provenance 消息显式绑定请求所属 goal，而不是碰巧绑定 precheck 当下 cycle/goal。
            mid = self.console.ingest.inbound(
                connector=connector, raw_text=raw, idempotency_key=idem,
                goal_id=goal_id, goal_ver=goal_ver,
                session_ref=FILE_REQUEST_ACTION_SESSION_REF)
            stored = self.console.daemon.query_one(
                "SELECT raw_text,goal_id,goal_ver,session_ref FROM interaction_message WHERE id=?", (mid,))
            if stored != (raw, goal_id, goal_ver, FILE_REQUEST_ACTION_SESSION_REF):
                raise IdempotencyCollisionError(
                    f"file_request action idempotency_key 已绑定其他消息: {idem}")
            self._ensure_action_classification(mid)
            if self._has_action_failure(mid):
                self._clear_attempt(idem)
                return "poison"

            expected_terminal = "resolved" if action in ("resolve", "approve") else "cancelled"
            if status != "pending":
                if status == expected_terminal and resolved_mid == mid:
                    self._clear_attempt(idem)            # 游标丢失/终态提交后崩溃：同 provenance 重放 no-op
                    return "ok"
                raise ValueError(
                    f"request {rid} 非 pending（{status}，resolved_message_id={resolved_mid}），不可 {action}")
            if self.file_requests is None:
                raise RuntimeError("ConsoleInboxIngest 未装配 FileRequestService，不能消费文件请求动作")
            if self._attempts.get(idem, 0) >= self._MAX_ATTEMPTS:
                if self._record_action_failure(mid, "file request action 持久基础设施/文件故障，未执行"):
                    self._clear_attempt(idem)
                    return "poison"
                return "retry"
            if action == "resolve":
                if self.system_root is None:
                    raise RuntimeError("ConsoleInboxIngest 未装配 system_root，不能校验 source_ref")
                # Capability fd stays open through the entire copy/hash/terminal
                # transition.  A rename/symlink swap after containment therefore
                # cannot retarget FileRequestService to another tree.
                with open_pinned_upload_ref(
                        source_ref, work_root=self.work_root,
                        system_root=self.system_root) as uploads:
                    self.file_requests.resolve(
                        request_id=rid, uploads_dir=uploads.proc_path,
                        resolved_message_id=mid)
            elif action == "approve":
                self.file_requests.approve(
                    request_id=rid, resolved_message_id=mid)
            else:
                self.file_requests.cancel(request_id=rid, reason=reason, resolved_message_id=mid)
        except IdempotencyCollisionError:
            logger.error("console_inbox file_request action idempotency collision (idem=%s) → 拒绝", idem,
                         exc_info=True)
            self._clear_attempt(idem)
            return "poison"                  # 旧 operation/message 不得被追加矛盾失败回执
        except RetryStateError:
            raise                             # resolve/cancel 可能已成功；重放按 resolved_message_id 收敛
        except (sqlite3.OperationalError, OSError, RuntimeError):
            attempts = self._bump(idem)
            logger.log(logging.ERROR if attempts >= self._MAX_ATTEMPTS else logging.WARNING,
                       "console_inbox file_request action 可重试故障 (idem=%s, r%s, 第%d次)；"
                       "不丢动作、不推进游标", idem, rid, attempts, exc_info=True)
            if attempts >= self._MAX_ATTEMPTS and mid is not None and self._record_action_failure(
                    mid, "file request action 持久基础设施/文件故障，未执行"):
                self._clear_attempt(idem)
                return "poison"
            return "retry"
        except (ValueError, sqlite3.IntegrityError) as e:
            # 不存在/已终态/goal provenance/路径/约束错误都是终态坏动作；请求若仍 pending 会继续阻断并在 UI 外显。
            logger.warning("console_inbox file_request action 被拒 (idem=%s, action=%s, r%s)",
                           idem, action, rid, exc_info=True)
            if mid is not None and not self._record_action_failure(mid, f"file request action 被拒：{e}"):
                return "retry"
            self._clear_attempt(idem)
            return "poison"
        self._clear_attempt(idem)
        return "ok"

    def _answer_query(self, idem: str, mid: int, *, special: Optional[str] = None) -> str:
        """query 应答。

        模板路径保持 reply-durable 才推进。Codex 路径在 ``enqueue_query`` 已原子落 active runner intent
        （或已落 terminal reply）后即可推进；后台调用不持 DB，完成由后续 main-writer ``poll`` 收口。
        """
        if special not in (None, "continue_running"):
            raise ValueError(f"未知 interaction query special: {special!r}")
        last_error: Optional[BaseException] = None
        try:
            has_terminal = (self.mediator.has_terminal_query_reply(mid)
                            if self.mediator.async_enabled else self._has_reply(mid))
            if has_terminal:                   # 已答（ACK 不算 async 终态）→不重复回复
                self._clear_attempt(idem)
                return "ok"
            if self.mediator.async_enabled:
                # 返回 accepted/recovered/rejected/completed 时均已有可重放的权威持久态；若 prepare/DB
                # 失败则抛到有限重试，绝不先推进 cursor。
                self.mediator.enqueue_query(message_id=mid)
                self._clear_attempt(idem)
                return "ok"
            if self._attempts.get(idem, 0) < self._MAX_ATTEMPTS:
                self.mediator.handle_query(message_id=mid)   # 未答 → 应答（写 interaction_reply）
                self._clear_attempt(idem)
                return "ok"
        except RetryStateError:
            raise                             # reply 可能已 durable；保留 cursor 后按 reply-exists 重放
        except sqlite3.OperationalError as error:       # 查 reply / 应答的瞬时故障 → 落有限重试
            last_error = error
        except Exception as error:                      # noqa: BLE001 —— 卡片未发布/应答器故障等 → 有限重试
            last_error = error
        if self._attempts.get(idem, 0) < self._MAX_ATTEMPTS:
            if self._bump(idem) < self._MAX_ATTEMPTS:
                logger.warning("console_inbox query 应答可重试 (message_id=%s, 第%d次)",
                               mid, self._attempts[idem], exc_info=True)
                return "retry"
        logger.error("console_inbox query 应答超重试上限 (message_id=%s) → 写终态失败回执", mid, exc_info=True)
        try:                                   # 终态失败回执：写一条 reply（存在→守卫防再答）。查/写任一失败 → 不推进（no-loss）
            has_terminal = (self.mediator.has_terminal_query_reply(mid)
                            if self.mediator.async_enabled else self._has_reply(mid))
            if not has_terminal:
                if self.mediator.async_enabled:
                    self.mediator.terminalize_query_prepare_failure(
                        message_id=mid,
                        cause=last_error or RuntimeError("query prepare retries exhausted"))
                else:
                    self.console.ingest.ack(
                        message_id=mid,
                        reply_text="（应答暂不可用：状态卡未发布或应答器故障，请稍后重试或直接查看各标签页）",
                        reply_role="final-template")
        except Exception:                      # noqa: BLE001 —— 连终态回执/查 reply 都失败（DB 持续故障）→ **不推进**、下轮再来
            logger.warning("console_inbox 终态回执写入/查询失败 (message_id=%s) → 不推进游标", mid, exc_info=True)
            return "retry"
        self._clear_attempt(idem)              # 有 durable reply（原答或终态回执）→ 推进
        return "ok"

    def _bump(self, idem: str) -> int:
        previous = self._attempts.get(idem)
        self._attempts[idem] = (previous or 0) + 1
        try:
            self._persist_attempts()
        except Exception:
            if previous is None:
                self._attempts.pop(idem, None)
            else:
                self._attempts[idem] = previous
            raise
        return self._attempts[idem]

    def _record_action_failure(self, mid: int, message: str) -> bool:
        """给 HTTP 已确认入队、但权威迁移终态拒绝的动作落一次可见回执；失败则调用方不得推进 cursor。"""
        try:
            if not self._has_action_failure(mid):
                self.console.ingest.ack(message_id=mid,
                                        reply_text=(_ACTION_FAILURE_PREFIX + message)[:2_000])
            return True
        except Exception:                      # noqa: BLE001 —— DB 写失败时保持该 spool 行待重试
            logger.warning("console_inbox action 失败回执落库失败 (message_id=%s)", mid, exc_info=True)
            return False

    def _has_action_failure(self, mid: int) -> bool:
        return self.console.daemon.query_one(
            "SELECT 1 FROM interaction_reply WHERE message_id=? AND reply_text LIKE ? LIMIT 1",
            (mid, _ACTION_FAILURE_PREFIX + "%")) is not None

    def _has_reply(self, mid: int) -> bool:
        return self.console.daemon.query_one(
            "SELECT 1 FROM interaction_reply WHERE message_id=? LIMIT 1", (mid,)) is not None
