"""ConsoleInboxIngest —— 人类控制台入站桥（步⑨ CP9.3）。

console_server（独立只读进程）把运维在控制台敲的命令 append 到 `<work>/state/console_inbox.jsonl`
（连接器缓冲、**非权威**）。本模块在 run 进程的 **precheck 边界**把这些行 ingest 进权威入站链：

  未消费行 → Console.handle_inbound（幂等：写 interaction_message + 分类落 directive/note）
           → intent=query 再 Mediator.handle_query（写 interaction_reply 应答）

**幂等与游标**：correctness 由持久层保证，游标 `console_inbox.cursor`（**已消费的已提交行数**，line-index）是纯优化、
可丢：
- directive/note：interaction_message UNIQUE(connector, idempotency_key) → 重复 ingest 命中既有 message、不重复建。
- query：handle_query **非幂等**（每调一次写一条 interaction_reply）——故应答前先查「该 message 是否已有 reply」，
  有则跳过。query-once 语义因此落在**持久层（reply 存在性）**而非游标上，游标丢失/崩溃重放都不会重复回复。
- 游标用**已提交行数**而非 max-seq：坏 JSON / 缺序号的已提交行也能被「跳过并推进」（行数照进），不会每拍重扫重复告警。
  （console_inbox.jsonl 是 append-only，行索引稳定；torn-tail 未终止行不计入，下轮补齐再消费。）

**健壮性（入站是辅助面，绝不拖垮自动推进主循环）**：
- **顶层兜底** try/except：读 spool / 写游标的 I/O 故障也不崩推进，记 warning 返回，下轮再来。
- **有限重试**：瞬时故障（DB locked/busy、卡片尚未发布 FileNotFoundError 等）→ 停批、不推进游标，下轮 precheck 重试；
  连续 `_MAX_ATTEMPTS` 次仍败 → 判**持久故障**、按终态处理并推进（防单条卡死饿死整队）：
    · handle_inbound 持久败 → 跳过并推进（raw 仍 durable 在 spool 供取证）；
    · query 应答持久败 → 写一条**终态失败回执**（reply 存在→守卫防再答），推进——既不漏答、不双答、也不饿死。
- **毒消息**（坏 JSON / 非 OperationalError 的 handle_inbound 异常）→ 记 warning、跳过并推进。

**并发/单写**：console_server 只 append spool + mode=ro 读 DB；run 进程 precheck 独占 cursor + DB 写（单写纪律不破）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ConsoleInboxIngest:
    """把 console_inbox.jsonl 未消费行 ingest 进 Console/Mediator（precheck 边界调用；见模块 docstring）。"""

    _MAX_ATTEMPTS = 5                          # 瞬时故障自愈窗口；超限=持久故障 → 终态处理并推进（防饥饿）

    def __init__(self, console, mediator, work_root: str):
        self.console = console
        self.mediator = mediator
        state = Path(work_root) / "state"
        self.inbox = state / "console_inbox.jsonl"
        self.cursor_path = state / "console_inbox.cursor"
        # idempotency_key → 已重试次数（进程内）。no-loss/no-dup 不受重启影响（reply 存在性守卫 + 不推进兜底）；
        # 唯一弱化：若 run 进程在达上限前**反复重启**，同一持久故障消息的 give-up 计数归零 → 理论上可长期阻塞后续行。
        # 但持久故障通常意味 DB/系统整体已停（那时推进主循环本身也停，阻塞 ingest 无害）→ v1 不跨重启持久化计数（留待需要时）。
        self._attempts: dict = {}

    def _cursor(self) -> int:
        """已消费的已提交行数（无游标文件 / 坏内容 → 0，即从头 ingest，靠幂等兜底）。"""
        try:
            return int(self.cursor_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return 0

    def _set_cursor(self, consumed_lines: int) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(str(consumed_lines), encoding="utf-8")

    def ingest(self, cyc: Any = None) -> int:
        """消费 cursor 之后所有已提交行；返回本次处理条数。cyc=当前 cycle（有则把 directive 绑到该轮）。
        **顶层兜底**：读 spool / 写游标的 I/O 故障也不得崩推进主循环（入站是辅助面）——记 warning 返回，下轮再来。"""
        try:
            return self._ingest(cyc)
        except Exception:                      # noqa: BLE001 —— read_text/_set_cursor 等边界 I/O 故障兜底（例：磁盘满、路径被替换）
            logger.warning("console_inbox ingest 顶层异常，跳过本轮", exc_info=True)
            return 0

    def _ingest(self, cyc: Any) -> int:
        if not self.inbox.exists():
            return 0
        text = self.inbox.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if lines and not text.endswith("\n"):
            lines = lines[:-1]                 # 末行未换行终止 = console_server append 中途（未 committed）→ 丢，下轮再来
        cursor = self._cursor()
        cycle_id: Optional[str] = getattr(cyc, "cycle_id", None)
        processed = 0
        consumed = cursor                      # 已消费的已提交行数（line-index 游标）
        for i in range(cursor, len(lines)):
            line = lines[i].strip()
            if not line:
                consumed = i + 1               # 空行照消费（推进行数）
                continue
            outcome = self._dispatch(line, cycle_id)
            if outcome == "retry":
                break                          # 瞬时故障：停在此行前、不推进游标越过它，下轮 precheck 重试（不丢消息）
            consumed = i + 1                   # ok / poison（含坏 JSON）→ 行数照进（毒消息不永久阻塞、不重复告警）
            if outcome == "ok":
                processed += 1
        if consumed > cursor:
            self._set_cursor(consumed)
        return processed

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
        if not idem:                           # 无幂等键且无 seq → 无法安全去重 → 判毒（跳过并推进），console_server 恒带二者
            logger.warning("console_inbox 记录缺 idempotency_key/seq，跳过: %r", str(rec)[:120])
            return "poison"
        try:
            res = self.console.handle_inbound(connector=connector, raw_text=raw,
                                              idempotency_key=idem, cycle_id=cycle_id)
        except sqlite3.OperationalError:       # DB locked/busy 等瞬时故障 → 有限重试（不推进）；超限 → 跳过并推进（raw durable 在 spool 供取证）
            if self._bump(idem) < self._MAX_ATTEMPTS:
                logger.warning("console_inbox ingest 可重试故障 (idem=%s, 第%d次)", idem, self._attempts[idem], exc_info=True)
                return "retry"
            logger.error("console_inbox ingest 超重试上限 (idem=%s) → 跳过并推进（raw 仍 durable）", idem, exc_info=True)
            self._attempts.pop(idem, None)
            return "poison"
        except Exception:                      # noqa: BLE001 —— 其余异常=内容/逻辑坏 → 跳过并推进（raw durable 在 spool 供取证）
            logger.warning("console_inbox ingest 失败跳过 (idem=%s, connector=%s)", idem, connector, exc_info=True)
            return "poison"
        if res.get("intent") == "query" and res.get("message_id") is not None:
            return self._answer_query(idem, res["message_id"])
        self._attempts.pop(idem, None)         # 成功 → 清计数
        return "ok"

    def _answer_query(self, idem: str, mid: int) -> str:
        """query 应答（**no-loss 不变量：只有当该 message 有 durable interaction_reply 时才推进游标**）。
        已答→推进（不重复）；未答→应答；应答失败→有限重试；超限→写终态失败回执，**仅回执 durable 写入才推进**，
        否则 'retry'（DB 持续故障时不推进 ingest 无害——那时推进主循环本身也已停）。"""
        try:
            if self._has_reply(mid):           # 已答（并发/前次/重放）→ 推进，不重复回复
                self._attempts.pop(idem, None)
                return "ok"
            self.mediator.handle_query(message_id=mid)   # 未答 → 应答（写 interaction_reply）
            self._attempts.pop(idem, None)
            return "ok"
        except sqlite3.OperationalError:       # 查 reply / 应答的瞬时故障 → 落有限重试
            pass
        except Exception:                      # noqa: BLE001 —— 卡片未发布(FileNotFoundError)/应答器故障等 → 落有限重试
            pass
        if self._bump(idem) < self._MAX_ATTEMPTS:
            logger.warning("console_inbox query 应答可重试 (message_id=%s, 第%d次)", mid, self._attempts[idem], exc_info=True)
            return "retry"
        logger.error("console_inbox query 应答超重试上限 (message_id=%s) → 写终态失败回执", mid, exc_info=True)
        try:                                   # 终态失败回执：写一条 reply（存在→守卫防再答）。查/写任一失败 → 不推进（no-loss）
            if not self._has_reply(mid):
                self.console.ingest.ack(message_id=mid,
                                        reply_text="（应答暂不可用：状态卡未发布或应答器故障，请稍后重试或直接查看各标签页）")
        except Exception:                      # noqa: BLE001 —— 连终态回执/查 reply 都失败（DB 持续故障）→ **不推进**、下轮再来
            logger.warning("console_inbox 终态回执写入/查询失败 (message_id=%s) → 不推进游标", mid, exc_info=True)
            return "retry"
        self._attempts.pop(idem, None)         # 有 durable reply（原答或终态回执）→ 推进
        return "ok"

    def _bump(self, idem: str) -> int:
        self._attempts[idem] = self._attempts.get(idem, 0) + 1
        return self._attempts[idem]

    def _has_reply(self, mid: int) -> bool:
        return self.console.daemon.query_one(
            "SELECT 1 FROM interaction_reply WHERE message_id=? LIMIT 1", (mid,)) is not None
