"""InteractionIngest —— 人机入站的 M1 降级实现（§4.6.2 / §7.2 特性④ M1 行：durable 入站 + ACK + 隔离）。

M1 只做「durable 入站 + 模板 ACK + 文件请求单登记」，**不触发真实 query responder、不改 route/decision/question**
（隔离铁律，§4.6.2「query/reply/ACK 不写 decision」，护 P1）——三分类 + 中介线程 + grounding + 通知矩阵是 M5；
pending `interaction_request` 在 M1–M3 **不触发真实通知/全局等待**（那些机制 M5 才有）。

写路径经 WriteDaemon（单写连接，§6.6）。所有 interaction_* 表 append-only，自成审计流、不污染研究账本。
隔离边界（M1c 验收，见 test_isolation_m1c）：入站/ACK/请求单**不产 decision、不改 question/cycle/route**——
由「本类只写 interaction_* 表」在实现上保证，用例断言之。
"""
from __future__ import annotations

import hashlib
from typing import Optional

from .ids import cnum as _cnum, qnum as _qnum   # 前缀校验解码
from .writedaemon import WriteDaemon


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class InteractionIngest:
    def __init__(self, daemon: WriteDaemon):
        self.daemon = daemon

    def inbound(self, *, connector: str, raw_text: str, idempotency_key: str,
                goal_id: Optional[int] = None, goal_ver: Optional[int] = None,
                cycle_id: Optional[str] = None, session_ref: Optional[str] = None) -> int:
        """唯一权威入站记录：写 raw 不可变 interaction_message（append-only）。
        幂等：同 (connector, idempotency_key) 已入则返回既有 id（durable JSONL 只是 connector 缓冲、非权威）。"""
        if (goal_id is None) != (goal_ver is None):   # DDL CHECK((goal_id IS NULL)=(goal_ver IS NULL)) 前置为干净拒
            raise ValueError("goal_id 与 goal_ver 须同为 None 或同非 None")
        if session_ref is not None and (
                not isinstance(session_ref, str) or not session_ref
                or len(session_ref) > 256
                or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in session_ref)):
            raise ValueError("session_ref 须为 1–256 字符且不得含控制字符")
        with self.daemon.transaction() as conn:
            existing = conn.execute("SELECT id FROM interaction_message WHERE connector=? AND idempotency_key=?",
                                    (connector, idempotency_key)).fetchone()
            if existing:
                return existing[0]
            return conn.execute(
                "INSERT INTO interaction_message(connector,conversation_id,session_ref,goal_id,goal_ver,cycle_id,"
                "raw_text,raw_hash,idempotency_key) VALUES (?,NULL,?,?,?,?,?,?,?)",
                (connector, session_ref, goal_id, goal_ver, _cnum(cycle_id) if cycle_id else None,
                 raw_text, _sha(raw_text), idempotency_key)
            ).lastrowid

    def ack(self, *, message_id: int, reply_text: str, snapshot_cycle: Optional[str] = None) -> int:
        """模板 ACK（带外回话；append-only）：写 interaction_reply(responder_kind='template')。
        **不写 decision**（§4.6.2 P1）；不改任何研究状态。真 codex 只读应答器 = M5。"""
        with self.daemon.transaction() as conn:
            return conn.execute(
                "INSERT INTO interaction_reply(message_id,reply_ref,reply_hash,reply_text,snapshot_cycle,responder_kind) "
                "VALUES (?,?,?,?,?,'template')",
                (message_id, f"reply:{message_id}", _sha(reply_text), reply_text,
                 _cnum(snapshot_cycle) if snapshot_cycle else None)
            ).lastrowid

    def create_file_request(self, *, goal_id: int, goal_ver: int, stage: str, summary_md: str,
                            items_json: str, request_hash: str, cycle_id: Optional[str] = None,
                            question_id: Optional[str] = None) -> int:
        """文件请求单登记：interaction_request(pending)。同一 pending attempt 以 (goal_id, request_hash)
        幂等；同 hash 已有终态时拒绝原样重提（上层须消费 resolved/cancelled 回执或改变请求条件），
        防无状态工人形成终态→同单重提循环。同 goal 至多一 pending（uq_ireq_one_pending，DDL 焊）。"""
        with self.daemon.transaction() as conn:
            # 幂等锚只覆盖 pending attempt；resolved/cancelled 是不可变历史，不能冒充下一次等待。
            existing = conn.execute(
                "SELECT id FROM interaction_request WHERE goal_id=? AND request_hash=? AND status='pending' "
                "ORDER BY id DESC LIMIT 1",
                (goal_id, request_hash)).fetchone()
            if existing:
                return existing[0]
            terminal = conn.execute(
                "SELECT id,status FROM interaction_request WHERE goal_id=? AND request_hash=? "
                "AND status<>'pending' ORDER BY id DESC LIMIT 1", (goal_id, request_hash)).fetchone()
            if terminal:
                raise ValueError(f"同 hash 文件请求已有终态 attempt #{terminal[0]}（{terminal[1]}），不得原样重提")
            return conn.execute(
                "INSERT INTO interaction_request(goal_id,goal_ver,cycle_id,question_id,stage,status,summary_md,"
                "items_json,request_hash) VALUES (?,?,?,?,?,'pending',?,?,?)",
                (goal_id, goal_ver, _cnum(cycle_id) if cycle_id else None,
                 _qnum(question_id) if question_id else None, stage, summary_md, items_json, request_hash)
            ).lastrowid
