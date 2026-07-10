"""Console —— 人类控制台核心：保守分类器 + directive 生命周期（§4.6.2–4.6.4；M5 CP6.1）。

**保守铁律（§4.6.2）**：任何**可能改状态**的语句一律 directive/回显确认；只有纯状态查询走 query；
低置信 → unclear（回显请确认、**不自动答、不改状态、不产 directive**——DDL：intent='unclear' 时
directive_id 必空）。分类器 = 廉价关键词规则（确定性、可回放；仿 classify_turn_intent——语义分类升级
留 M6，保守面不变：词表未命中即 unclear，绝不猜）。

**润色≠raw 时序（硬，DDL trg_iclass_directive_prov）**：raw 原文不可变落 interaction_message；分类为
directive 时**先建 directive(status='pending')**（payload_json 携润色稿 + confirmed 标志 + 分类器
provenance），分类行插入时回指该 directive。**硬指令回显确认展示润色稿**——用户确认的是润色后语义；
确认不过 → status='rejected'（不消费）。**未确认硬指令 consume 拒**（§7.1 M5）。

**消费（§4.6.4）**：consume_directive 按 consume_at 时机由调用方触发（immediate/stage_boundary =
Advancer 前置检查点；reasoning_start = reasoning 轮始）——消费 = **单事务内**读校验（防 TOCTOU）+
最小状态效果 + DECISION(actor='human'，directive_id 回指；decision 不 FK interaction_message，
provenance 经 directive 间接回溯) + 条件更新 status='consumed'。**软指令可有理由不从**：
reject_directive(by_decision=True) 记 DECISION(理由) + status='rejected'。

**pause/resume 状态模型**：pause 的消费 = 进入暂停态（该 DECISION 即记账）；**阻断谓词
has_blocking_pause = 最近一次被消费的 pause/resume 是 pause**（按消费序 consumed_decision_id）——
阻断跨越 pause 消费后的全程，直到 resume 被消费解除；pending（含已确认未消费）不阻断，调用方须
先消费到期 directive 再查阻断（Advancer 前置检查顺序，CP6.3 接线）。resume 消费顺带把**早于它的**
pending pause 置 superseded（队列清理；晚到的 pause 保留、到时机再生效）。

其余效果：abort_cycle（在途轮 aborted，并原子释放 active 问题）、inject_question（open 问题，source='human'）、
prune_branch（decision(type=prune_branch) 先行再 dead_end，且**该决策即消费决策**——一次消费一条
人类决策，不重复记账）、note（按 consumed_cycle 真正编入下一次 reasoning ContextPack）。
`set_budget/reprioritize/goal_amend` 在对应运行时 override、选题约束、goal_amend reasoning-only 路由
闭合前**不得标 consumed**：precheck 会把它们终态 rejected，并以 DECISION 明示能力尚不可用，避免
“状态显示已应用、实际上无效果”的假控制面。

**P1**：query/reply/ACK 不写 decision；人机原文只在 interaction_*。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Callable, Dict, Optional

from .ids import cnum as _cnum
from .interaction import InteractionIngest
from .resource_limits import MAX_REASONING_DIRECTIVES_PER_CYCLE
from .writedaemon import WriteDaemon

# 指令词表：kind → (触发词, hardness, consume_at)。§4.6.4 表逐行对齐。
_DIRECTIVE_RULES = [
    ("pause",           ("暂停", "pause"),                       "hard", "immediate"),
    ("resume",          ("继续", "恢复", "resume"),              "hard", "immediate"),
    ("abort_cycle",     ("中止本轮", "中止当前轮", "abort"),     "hard", "immediate"),
    ("set_budget",      ("预算", "budget"),                      "hard", "stage_boundary"),
    ("inject_question", ("注入问题", "加个问题", "inject"),      "soft", "reasoning_start"),
    ("reprioritize",    ("优先", "pin", "降权", "提权"),         "soft", "reasoning_start"),
    ("prune_branch",    ("剪枝", "砍掉", "prune"),               "hard", "reasoning_start"),
    ("goal_amend",      ("改目标", "修订目标", "goal amend"),    "hard", "reasoning_start"),
    ("note",            ("备注", "note:", "注："),               "soft", "reasoning_start"),
]
# query 提示词只收**实义状态词**——裸疑问助词（吗/？/?）故意不收：礼貌式指令（"停掉好吗"）常带助词，
# 若据此归 query 会被静默只读作答而非进澄清环（保守铁律：宁 unclear 勿误 query）。
_QUERY_HINTS = ("现状", "进展", "进度", "状态", "结果", "为什么", "什么", "多少", "哪",
                "status", "why", "what", "how")

# console HTTP/spool 的 operation domain 必须进入权威 append-only message；只留在 JSONL 会在 cursor
# 丢失/跨端点 nonce 复用时失去判别力。复用冻结 DDL 的 session_ref，不新增 migration。
CONSOLE_MESSAGE_SESSION_REF = "console-op:message:v1"
DIRECTIVE_ACTION_SESSION_REF = "console-op:directive-action:v1"
FILE_REQUEST_ACTION_SESSION_REF = "console-op:file-request-action:v1"


class IdempotencyCollisionError(ValueError):
    """同一 connector nonce 已绑定另一份不可变入站内容/goal。"""


class DirectiveApplicationError(ValueError):
    """A durable directive is well-formed enough to audit but cannot be applied."""


def sanitize(text: str, max_len: int = 2000) -> str:
    """消毒（中介/应答器输入用）：去控制字符 + 截断。raw 永不改，此为衍生视图。"""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)[:max_len]


def directive_action_text(action: str, directive_id: int, *, reason: str = "") -> str:
    """显式 directive 控件动作的唯一不可变原文口径（server/ingest/事务终检共用）。"""
    if action == "confirm":
        return f"确认指令 d{directive_id}"
    if action == "reject":
        digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()
        return f"拒绝指令 d{directive_id} reason_sha256:{digest}"
    raise ValueError(f"directive action 非法: {action!r}")


def _hit(low: str, w: str) -> bool:
    """词命中：ASCII 词要求词边界（防 "pin"∈"opinion" 这类中缀假阳性——软指令假阳会污染 decision 台账）；
    CJK 词无空格分词、保持子串匹配。"""
    if w.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low) is not None
    return w in low


class KeywordClassifier:
    """廉价关键词保守分类（确定性）。返回 {intent, kind?, hardness?, consume_at?, polished?}。"""

    def classify(self, message: Dict[str, Any]) -> Dict[str, Any]:
        text = sanitize(str(message.get("raw_text", "")))
        low = text.lower()
        for kind, words, hardness, consume_at in _DIRECTIVE_RULES:
            if any(_hit(low, w) for w in words):
                # note 是 DDL 独立 intent（分类行 directive_id 必空）；其余指令词 → directive
                return {"intent": "note" if kind == "note" else "directive",
                        "kind": kind, "hardness": hardness, "consume_at": consume_at,
                        "polished": f"[{kind}] {text.strip()}"}   # 润色=规范化表述（真 Codex 润色=M6，确定性先行）
        if any(_hit(low, h) for h in _QUERY_HINTS) and text.strip():
            return {"intent": "query"}
        return {"intent": "unclear"}          # 词表未命中 → 不猜（保守铁律：绝不静默当 query/directive）


class Console:
    def __init__(self, daemon: WriteDaemon, classifier=None):
        self.daemon = daemon
        self.ingest = InteractionIngest(daemon)
        self.classifier = classifier or KeywordClassifier()

    # ---------------------------------------------------------------- 入站 --
    def handle_inbound(self, *, connector: str, raw_text: str, idempotency_key: str,
                       goal_id: Optional[int] = None, goal_ver: Optional[int] = None,
                       cycle_id: Optional[str] = None,
                       session_ref: Optional[str] = None) -> Dict[str, Any]:
        """durable 入站 → 恰一分类（幂等：message UNIQUE）→ directive/note 先建行再回指（DDL 时序）。
        返回 {message_id, intent, directive_id?, needs_confirmation?}。unclear：不自动答不产 directive
        （ACK 回显请确认由通知层出，CP6.3）。"""
        mid = self.ingest.inbound(connector=connector, raw_text=raw_text, idempotency_key=idempotency_key,
                                  goal_id=goal_id, goal_ver=goal_ver, cycle_id=cycle_id,
                                  session_ref=session_ref)
        # InteractionIngest 的 UNIQUE 只负责找回 message id；在读取既有 classification 或调用分类器前，
        # 必须先证明 replay 的不可变 payload 相同。否则“首事务只落 message 后崩溃”的窗口里，撞键 body
        # 可把自己的 directive 语义提交到另一条 raw message 上。cycle_id 不参与比较：传输重放可能跨
        # precheck/cycle 才到达，但仍应收敛到首次 durable message。
        stored = self.daemon.query_one(
            "SELECT connector,raw_text,raw_hash,goal_id,goal_ver,session_ref "
            "FROM interaction_message WHERE id=?", (mid,))
        expected_hash = "sha256:" + hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        if stored != (connector, raw_text, expected_hash, goal_id, goal_ver, session_ref):
            raise IdempotencyCollisionError(
                f"{connector} idempotency_key 已绑定其他不可变消息: {idempotency_key}")
        ex = self._existing_classification(mid)
        if ex:                                 # 幂等重放：分类恰一（UNIQUE），返回既有
            return ex
        c = self.classifier.classify({"raw_text": raw_text})
        ci = _cnum(cycle_id) if cycle_id else None
        try:
            with self.daemon.transaction() as conn:
                did = None
                if c["intent"] in ("directive", "note"):
                    kind = c.get("kind", "note")
                    hardness = c.get("hardness", "soft")
                    payload = {"polished": c.get("polished", sanitize(raw_text)),
                               "confirmed": hardness != "hard",     # 软指令免确认；硬指令须回显确认后置 true
                               "classifier": "keyword-v1"}
                    did = conn.execute(
                        "INSERT INTO directive(kind,hardness,status,consume_at,payload_json,created_cycle,"
                        "source_interaction_message_id) VALUES (?,?,'pending',?,?,?,?)",
                        (kind, hardness, c.get("consume_at", "reasoning_start"),
                         json.dumps(payload, ensure_ascii=False), ci, mid)).lastrowid
                # note 的分类行 directive_id 必空（DDL CHECK：仅 intent='directive' 携 id）；note 的 directive
                # 行仍建（§4.6.3：note → directive(note, 软)），provenance 经 source_interaction_message_id。
                conn.execute("INSERT INTO interaction_classification(message_id,intent,directive_id) VALUES (?,?,?)",
                             (mid, c["intent"], did if c["intent"] == "directive" else None))
        except sqlite3.IntegrityError:
            # 并发重放窗口：预检后、插入前别处已落分类 → UNIQUE(message_id) 冲突整体回滚（directive 同事务、
            # 不残留）；回读既有分类返回，不向上炸（幂等语义）
            ex = self._existing_classification(mid)
            if ex is None:
                raise
            return ex
        return {"message_id": mid, "intent": c["intent"], "directive_id": did,
                "needs_confirmation": bool(did) and c.get("hardness") == "hard" and c["intent"] == "directive"}

    def _existing_classification(self, mid: int) -> Optional[Dict[str, Any]]:
        """既有分类 → 幂等返回值（与首次返回**等价**，含 needs_confirmation——重放丢首次响应后调用方
        仍能据此触发确认 UI）；note 分类行 directive_id 必空，经 directive.source 回指找回。"""
        ex = self.daemon.query_one("SELECT intent, directive_id FROM interaction_classification WHERE message_id=?",
                                   (mid,))
        if ex is None:
            return None
        did = ex[1]
        if did is None and ex[0] == "note":
            row = self.daemon.query_one("SELECT id FROM directive WHERE source_interaction_message_id=?", (mid,))
            did = row[0] if row else None
        needs = False
        if did is not None and ex[0] == "directive":
            dr = self.daemon.query_one("SELECT hardness, status, payload_json FROM directive WHERE id=?", (did,))
            needs = bool(dr) and dr[0] == "hard" and dr[1] == "pending" and not json.loads(dr[2]).get("confirmed")
        return {"message_id": mid, "intent": ex[0], "directive_id": did, "needs_confirmation": needs}

    # ---------------------------------------------------------------- 确认 --
    @staticmethod
    def _validate_action_provenance(conn, *, directive_id: int, source_message_id: int,
                                    action_message_id: int, action: str, reason: str = "") -> None:
        """在最终状态迁移事务内验证控件消息，而不是信任上游调用者已经检查过。

        action message 必须是 ``unclear`` 分类、具有 deterministic raw，且 goal 绑定与 directive 的
        source message 完全一致（含 NULL/goal_ver）。这样任意既有消息、原 directive 源消息或跨 goal
        消息都不能冒充确认/拒绝 provenance。
        """
        action_row = conn.execute(
            "SELECT m.raw_text,m.goal_id,m.goal_ver,c.intent,c.directive_id,m.session_ref "
            "FROM interaction_message m LEFT JOIN interaction_classification c ON c.message_id=m.id "
            "WHERE m.id=?", (action_message_id,)).fetchone()
        if action_row is None:
            raise ValueError(f"{action} provenance 消息不存在: {action_message_id}")
        if action_row[0] != directive_action_text(action, directive_id, reason=reason):
            raise ValueError(f"directive {directive_id} {action} provenance 原文不符")
        if (action_row[3], action_row[4]) != ("unclear", None):
            raise ValueError(f"directive {directive_id} {action} provenance 须为 unclear 控件消息")
        if action_row[5] != DIRECTIVE_ACTION_SESSION_REF:
            raise ValueError(f"directive {directive_id} {action} provenance 操作域不符")
        source_goal = conn.execute(
            "SELECT goal_id,goal_ver FROM interaction_message WHERE id=?", (source_message_id,)).fetchone()
        if source_goal is None:
            raise ValueError(f"directive {directive_id} source provenance 消息不存在")
        if (action_row[1], action_row[2]) != source_goal:
            raise ValueError(f"directive {directive_id} {action} provenance 与 source goal 不一致")

    def confirm_directive(self, *, directive_id: int, confirm_message_id: int) -> None:
        """硬指令回显确认（用户确认的是润色稿语义）：payload.confirmed=true + 确认消息 provenance。
        directive 无 append-only 触发器（状态机表），UPDATE 合法；status 保持 pending（待时机消费）。
        读校验与更新同事务（防 TOCTOU，同 consume）。"""
        with self.daemon.transaction() as conn:
            row = conn.execute(
                "SELECT status,hardness,payload_json,source_interaction_message_id FROM directive WHERE id=?",
                               (directive_id,)).fetchone()
            if row is None:
                raise ValueError(f"directive 不存在: {directive_id}")
            if row[0] != "pending":
                raise ValueError(f"directive {directive_id} 非 pending（{row[0]}），不可确认")
            if row[1] != "hard":
                raise ValueError(f"directive {directive_id} 是软指令，无需回显确认")
            self._validate_action_provenance(
                conn, directive_id=directive_id, source_message_id=row[3],
                action_message_id=confirm_message_id, action="confirm")
            payload = json.loads(row[2])
            if payload.get("confirmed") is True:
                if payload.get("confirmation_message_id") == confirm_message_id:
                    return
                raise ValueError(f"directive {directive_id} 已由另一条消息确认")
            if payload.get("confirmed") is not False:
                raise ValueError(f"directive {directive_id} confirmed 字段损坏")
            payload["confirmed"] = True
            payload["confirmation_message_id"] = confirm_message_id
            n = conn.execute("UPDATE directive SET payload_json=? WHERE id=? AND status='pending'",
                             (json.dumps(payload, ensure_ascii=False), directive_id)).rowcount
            if n != 1:        # 兜底同 consume（同事务已校验，理论不可达）
                raise RuntimeError(f"directive {directive_id} 确认竞态：更新失败")

    def reject_directive(self, *, directive_id: int, reason: str, reject_message_id: Optional[int] = None,
                         by_decision: bool = False, cycle_id: Optional[str] = None) -> None:
        """确认不过（用户否掉润色稿）→ rejected 不消费；by_decision=True = **软指令系统有理由不从**
        （§4.6.4：须 DECISION 写明理由——此路记账；用户否决路不写 decision[P1：非研究决策]）。
        读校验与更新同事务（防 TOCTOU，同 consume）。"""
        with self.daemon.transaction() as conn:
            row = conn.execute(
                "SELECT status,hardness,payload_json,source_interaction_message_id FROM directive WHERE id=?",
                               (directive_id,)).fetchone()
            if row is None:
                raise ValueError(f"directive 不存在: {directive_id}")
            if row[0] != "pending":
                raise ValueError(f"directive {directive_id} 非 pending（{row[0]}），不可拒")
            if by_decision:
                if reject_message_id is not None:
                    raise ValueError("系统不从路径不得冒充用户拒绝 provenance")
                if row[1] != "soft":
                    raise ValueError("系统不从仅限软指令（硬指令绕过权衡直接生效，§4.6.4）")
                conn.execute("INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
                             "VALUES (?,?,'orchestrator','soft_directive_declined',?)",
                             (_cnum(cycle_id) if cycle_id else None, directive_id,
                              json.dumps({"reason": reason}, ensure_ascii=False)))
            else:
                if reject_message_id is None:
                    raise ValueError("用户拒绝须提供 reject_message_id provenance")
                self._validate_action_provenance(
                    conn, directive_id=directive_id, source_message_id=row[3],
                    action_message_id=reject_message_id, action="reject", reason=reason)
            # 用户拒绝路不写 decision（P1），但理由和控件消息 id 入 payload 供审计/幂等重放。
            payload = json.loads(row[2])
            payload["rejection_reason"] = reason
            if reject_message_id is not None:
                payload["rejection_message_id"] = reject_message_id
            n = conn.execute(
                "UPDATE directive SET status='rejected', payload_json=? WHERE id=? AND status='pending'",
                (json.dumps(payload, ensure_ascii=False), directive_id)).rowcount
            if n != 1:        # 兜底同 consume（同事务已校验，理论不可达）
                raise RuntimeError(f"directive {directive_id} 拒绝竞态：更新失败")

    def reject_unapplicable_directive(self, *, directive_id: int, reason: str,
                                      cycle_id: Optional[str] = None) -> None:
        """Terminalize a confirmed/due directive whose requested effect is unavailable.

        This is not a user rejection and is valid for hard as well as soft
        directives.  The explicit DECISION prevents an unsupported command
        from being advertised as ``consumed`` while also avoiding a permanent
        poison-pill at every precheck.
        """
        with self.daemon.transaction() as conn:
            row = conn.execute(
                "SELECT status,payload_json FROM directive WHERE id=?", (directive_id,)).fetchone()
            if row is None:
                raise ValueError(f"directive 不存在: {directive_id}")
            if row[0] == "rejected":
                return
            if row[0] != "pending":
                raise ValueError(f"directive {directive_id} 非 pending（{row[0]}），不可终态拒绝")
            payload = json.loads(row[1])
            if not isinstance(payload, dict):
                raise RuntimeError(f"directive {directive_id} payload 不是 JSON object")
            payload["rejection_reason"] = str(reason)[:2_000]
            payload["rejection_kind"] = "application_unavailable"
            dec = conn.execute(
                "INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
                "VALUES (?,?,'orchestrator','directive_application_rejected',?)",
                (_cnum(cycle_id) if cycle_id else None, directive_id,
                 json.dumps({"reason": str(reason)[:2_000]}, ensure_ascii=False))).lastrowid
            changed = conn.execute(
                "UPDATE directive SET status='rejected',payload_json=? "
                "WHERE id=? AND status='pending'",
                (json.dumps(payload, ensure_ascii=False), directive_id)).rowcount
            if changed != 1:
                raise RuntimeError(f"directive {directive_id} 终态拒绝竞态：更新失败")
            if dec is None:
                raise RuntimeError(f"directive {directive_id} 终态拒绝 DECISION 未落库")

    # ---------------------------------------------------------------- 消费 --
    def pending_directives(self, consume_at: str) -> list:
        """指定时机**当下可消费**的 directive id 序列（创建序）：软指令或已确认硬指令——未确认硬指令
        不进队（consume 会拒，进队只会让调度方稳定撞拒；其提醒走通知层，CP6.3）。Advancer 前置检查
        （immediate/stage_boundary）与 reasoning 轮始（reasoning_start）按此取。"""
        return [r[0] for r in self.daemon.query(
            "SELECT id FROM directive WHERE status='pending' AND consume_at=? "
            "AND (hardness='soft' OR json_extract(payload_json,'$.confirmed')) ORDER BY id", (consume_at,))]

    def has_blocking_pause(self) -> bool:
        """§4.4.1 前置检查：**最近一次被消费的 pause/resume 是 pause** → 暂停态，不发起新研究 Runner 调用。
        阻断从 pause 消费起持续到 resume 消费止；pending（含已确认）不阻断——调用方须先消费到期
        directive 再查阻断（前置检查顺序）。消费序 = consumed_decision_id（decision 自增，全局单调）。"""
        r = self.daemon.query_one("SELECT kind FROM directive WHERE status='consumed' AND kind IN ('pause','resume') "
                                  "ORDER BY consumed_decision_id DESC LIMIT 1")
        return bool(r) and r[0] == "pause"

    def consume_directive(self, *, directive_id: int, cycle_id: Optional[str] = None,
                          state=None) -> Dict[str, Any]:
        """按时机消费——**单事务内**读校验+效果+DECISION(actor='human', directive_id 回指)+条件更新
        consumed（读写同事务，WriteDaemon 单写串行 → 无 TOCTOU 窗口；条件更新 rowcount 兜底）。
        拒：非 pending；**硬指令未确认**（§7.1 M5「未确认硬指令 consume_directive 拒」）。
        cycle_id 可空（Advancer 前置检查在开轮前消费 immediate 指令时无在途轮；DECISION.cycle_id/
        consumed_cycle 本可空）。返回 {kind, effect} 供通知层（applied 事件，CP6.3）。"""
        ci = _cnum(cycle_id) if cycle_id else None
        with self.daemon.transaction() as conn:
            row = conn.execute(
                "SELECT kind,hardness,status,consume_at,payload_json FROM directive WHERE id=?",
                               (directive_id,)).fetchone()
            if row is None:
                raise ValueError(f"directive 不存在: {directive_id}")
            kind, hardness, status, consume_at, payload_raw = row
            if status != "pending":
                raise ValueError(f"directive {directive_id} 非 pending（{status}），不可消费")
            payload = json.loads(payload_raw)
            if hardness == "hard" and not payload.get("confirmed"):
                raise ValueError(f"硬指令 {directive_id}（{kind}）未经回显确认，不可消费（§4.6.2 润色确认硬门）")
            if kind in ("set_budget", "reprioritize", "goal_amend"):
                raise DirectiveApplicationError(
                    f"{kind} 尚未接入其 reference 要求的真实状态语义，拒绝伪装为已应用")
            # Only reasoning_start directives are compiled into this cycle's reasoning pack.  Operational
            # immediate controls (especially resume/abort) must remain available even after the prompt budget
            # is full, otherwise a note flood could make a paused cycle impossible to resume.
            if ci is not None and consume_at == "reasoning_start":
                consumed_for_cycle = conn.execute(
                    "SELECT count(*) FROM directive WHERE status='consumed' AND consumed_cycle=? "
                    "AND consume_at='reasoning_start'",
                    (ci,)).fetchone()[0]
                if consumed_for_cycle >= MAX_REASONING_DIRECTIVES_PER_CYCLE:
                    raise DirectiveApplicationError(
                        f"cycle c{ci} 人类 directive 已达上下文安全上限 "
                        f"{MAX_REASONING_DIRECTIVES_PER_CYCLE}；本条未执行")
            effect: Dict[str, Any] = {"kind": kind}
            dec = None            # prune_branch 复用其 prune 决策为消费决策（一次消费恰一条人类决策）
            if kind == "inject_question":
                # goal_id=1 + MAX(version) = 全库单目标约定（statestore/advancer 同口径）；多目标=系统级改造
                if conn.execute("SELECT 1 FROM goal WHERE id=1 LIMIT 1").fetchone() is None:
                    raise DirectiveApplicationError("inject_question 时当前 goal 不存在")
                qid = conn.execute(
                    "INSERT INTO question(goal_id,goal_ver,born_goal_ver,text,status,source) "
                    "SELECT 1, MAX(version), MAX(version), ?, 'open', 'human' FROM goal WHERE id=1",
                    (payload.get("polished", ""),)).lastrowid
                effect["question_id"] = f"q{qid}"
            elif kind == "prune_branch":
                qref = payload.get("question_id")
                if not qref:
                    raise DirectiveApplicationError("prune_branch 需 payload.question_id（润色/确认阶段补齐）")
                try:
                    qi = int(str(qref)[1:]) if str(qref).startswith("q") else int(qref)
                except (TypeError, ValueError):
                    raise DirectiveApplicationError(f"prune_branch question_id 非法: {qref!r}") from None
                qs = conn.execute("SELECT status FROM question WHERE id=?", (qi,)).fetchone()
                if qs is None or qs[0] not in ("open", "inconclusive"):
                    raise DirectiveApplicationError(
                        f"prune_branch 只允许 open/inconclusive 目标: {qref}"
                        f"（{qs[0] if qs else '缺失'}）")
                effect["question_id"] = f"q{qi}"
                dec = conn.execute("INSERT INTO decision(cycle_id,question_id,directive_id,actor,type,payload_json) "
                                   "VALUES (?,?,?,'human','prune_branch',?)",
                                   (ci, qi, directive_id,
                                    json.dumps({"effect": effect, "polished": payload.get("polished")},
                                               ensure_ascii=False))).lastrowid
                conn.execute("UPDATE question SET status='dead_end' WHERE id=?", (qi,))   # trg_q_deadend 要求 decision 先行
            elif kind == "abort_cycle":
                # 单轮在途约定（Advancer 串行推进）：非终态轮至多一个，即"本轮"；多轮并发=系统级改造
                cur = conn.execute("SELECT id,active_question_id FROM cycle "
                                   "WHERE status NOT IN ('done','failed','aborted') "
                                   "ORDER BY id LIMIT 1").fetchone()
                if cur:
                    active_question_id = cur[1]
                    if active_question_id is not None:
                        released = conn.execute(
                            "UPDATE question SET status='open' WHERE id=? AND status='active'",
                            (active_question_id,)).rowcount
                        if released != 1:
                            raise DirectiveApplicationError(
                                f"cycle c{cur[0]} active_question_id=q{active_question_id} "
                                "未指向 active 问题；abort 未执行，需先修复权威状态漂移")
                        effect["released_question"] = f"q{active_question_id}"
                    conn.execute(
                        "UPDATE cycle SET status='aborted', active_question_id=NULL, "
                        "finished_at=CURRENT_TIMESTAMP WHERE id=?", (cur[0],))
                    effect["aborted_cycle"] = f"c{cur[0]}"
            # pause：消费即进入暂停态（阻断语义在 has_blocking_pause，按消费序判定），无额外行内效果
            elif kind == "resume":
                # 队列清理：把**早于本 resume 的** pending pause 置 superseded（用户指令有序：晚到的 pause
                # 是新诉求、保留到其时机再生效）。解除阻断本身由消费序体现（本 resume 成为最近消费）。
                for r in conn.execute("SELECT id FROM directive WHERE status='pending' AND kind='pause' AND id<?",
                                      (directive_id,)).fetchall():
                    conn.execute("UPDATE directive SET status='superseded' WHERE id=?", (r[0],))
                    effect.setdefault("superseded_pause", []).append(r[0])
            elif kind == "note":
                # compiler 按 consumed_cycle 把该注解注入同一 reasoning ContextPack。
                effect["published_to_reasoning_cycle"] = cycle_id
            if dec is None:
                dec = conn.execute("INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
                                   "VALUES (?,?,'human',?,?)",
                                   (ci, directive_id, f"directive_{kind}",
                                    json.dumps({"effect": effect, "polished": payload.get("polished")},
                                               ensure_ascii=False))).lastrowid
            claimed = conn.execute("UPDATE directive SET status='consumed', consumed_cycle=?, consumed_decision_id=? "
                                   "WHERE id=? AND status='pending'", (ci, dec, directive_id)).rowcount
            if claimed != 1:      # 理论不可达（同事务已校验+单写串行）；兜底防未来改动引入窗口
                raise RuntimeError(f"directive {directive_id} 消费竞态：claim 失败")
        return effect
