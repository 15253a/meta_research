"""status_card 构建器（§4.6.6 封闭字段清单）—— 人机控制台的**阶段边界发布派生卡**。

**性质**：派生快照，可从 DB 真相重建 → **不在核心 DDL**（附录 A 无此表，§4.6.2）。此处 = **构建器**
（M2：封闭字段集、确定性可测）+ **SqliteStatusPublisher 原子发布器**（M5 CP6.2：advance 阶段边界调用，
tmp→rename 覆盖 latest 文件；Mediator/应答器只读该发布快照，不读在途 DB）。

**封闭字段**（§4.6.6，顺序固定、集合封闭——不多不少）：
  snapshot_cycle · goal(版本摘要) · active_question(问题卡) · cycle_status/route
  · selection(intent + 最近 selection DECISION 摘要) · budget(B(t)/本轮已花/全局剩余)
  · counts(open/inconclusive) · heartbeat_ref · pending_file_request(pending 文件请求摘要)

**字段真源现状**：
  - selection.latest_decision：**已接线（M5 CP6.2）**= 本 cycle 作用域最近 decision 摘要（{id,actor,type}）。
  - budget.global_remaining：policy 只有单轮 B_max，无全局会话上限（会话级旋钮，非核心 DDL）→ None。
  - heartbeat_ref：heartbeat/outbox 是实现层幂等队列、非核心 DDL（§4.6.2）→ None（CP6.3 outbox 落）。

**纯函数 / 可测**：不调 wall-clock。pending 请求「已等待时长」= 展示时刻 − created_at，由控制台在展示时算；
M2 只给锚点 created_at（不在卡内假造时长——精确换算需全系统时区/格式约定，M3 定）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .budgeting import compute_budget
from .ids import cnum as _cnum


def build_status_card(conn, *, cycle_id: str, policy: Dict[str, Any], goal_body_md: str) -> Dict[str, Any]:
    """从 DB 真相构建 status_card（封闭字段集）。conn 须为专用只读连接（isolation_level=None，同 compiler 约定）。
    整卡在一个读事务内构建（钉一致快照，杜绝混态：cycle 取 A 态、questions 取 B 态）。

    **goal_body_md 契约**（同 compiler 的 goal_body_md）：须是**本 cycle 当前 goal_ver 绑定**的目标正文——
    由调用方（M3 advance）按 cycle.goal_id/goal_ver 解析后传入；本函数不跨版校验（它不在 BEGIN 快照内）。
    M3 接线务必传版本正确的正文，勿跨 goal/version 复用同一参数（否则 goal.summary 会串版）。"""
    ci = _cnum(cycle_id)
    conn.isolation_level = None            # 本函数掌控事务（钉读快照）；调用方应传专用读连接
    conn.execute("BEGIN")                  # 钉一致读快照（只读，COMMIT 即释放）
    try:
        cyc = conn.execute(
            "SELECT goal_id, goal_ver, active_question_id, status, route, cost_total, "
            "next_question_id, next_intent FROM cycle WHERE id=?", (ci,)).fetchone()
        if cyc is None:
            raise ValueError(f"cycle 不存在: {cycle_id}")
        goal_id, goal_ver, aq, cstatus, route, cost_total, next_q, next_intent = cyc

        active_question = None
        if aq is not None:
            q = conn.execute("SELECT text, status, visit_count FROM question WHERE id=?", (aq,)).fetchone()
            active_question = {"id": f"q{aq}", "text": q[0], "status": q[1], "visit_count": q[2]}

        # selection：权威状态取自 cycle.next_*（persist_selection 只更新 cycle、不写专门 selection decision）。
        # latest_decision（M5 CP6.2 接线）= **本 cycle 作用域**最近一条 decision 摘要（decision.cycle_id=ci，
        # 非全局 LIMIT 1——早前内审 SHOULD：全局最新会跨 goal/轮串卡）。reasoning 落的 create_root/decompose/
        # answer_review、consume_directive 落的 directive_* 都在此可见；无则诚实 None（如轮刚开）。
        ld = conn.execute("SELECT id, actor, type FROM decision WHERE cycle_id=? ORDER BY id DESC LIMIT 1",
                          (ci,)).fetchone()
        selection = {
            "intent": next_intent,
            "next_question_id": f"q{next_q}" if next_q is not None else None,   # 权威选择状态（"选了哪题"）
            "latest_decision": {"id": ld[0], "actor": ld[1], "type": ld[2]} if ld else None,
        }

        # §4.6.6 预算三元（不多不少）：B(t) / 本轮已花 / 全局剩余
        budget = {
            "B_t": compute_budget(conn, policy["budget"]),
            "cycle_spent": float(cost_total),      # 本轮已花 = cycle.cost_total
            "global_remaining": None,              # M2: 无全局会话上限（会话级旋钮，非核心 DDL）→ 无从算剩余
        }

        counts = {"open": 0, "inconclusive": 0}
        for st, n in conn.execute(
                "SELECT status, count(*) FROM question WHERE goal_id=? AND status IN ('open','inconclusive') "
                "GROUP BY status", (goal_id,)).fetchall():
            counts[st] = n

        pending_file_request = _pending_file_request(conn, goal_id)

        return {
            "snapshot_cycle": cycle_id,
            "goal": {"id": goal_id, "ver": goal_ver, "summary": _first_line(goal_body_md)},
            "active_question": active_question,
            "cycle_status": cstatus,
            "route": route,
            "selection": selection,
            "budget": budget,
            "counts": counts,
            "heartbeat_ref": None,        # M2: heartbeat/outbox = 实现层队列、非核心 DDL（M3 落）
            "pending_file_request": pending_file_request,
        }
    finally:
        conn.execute("COMMIT")            # 结束只读快照


def _pending_file_request(conn, goal_id) -> Optional[Dict[str, Any]]:
    """pending 文件请求摘要（§4.6.8；每 goal 至多一条 pending，UNIQUE(goal_id) WHERE status='pending'）。
    答「系统为什么停住」：request_id / 条目数 / created_at（等待起点锚；时长由控制台展示时算）。"""
    r = conn.execute(
        "SELECT id, items_json, created_at FROM interaction_request "
        "WHERE goal_id=? AND status='pending' ORDER BY id LIMIT 1", (goal_id,)).fetchone()
    if r is None:
        return None
    req_id, items_json, created_at = r
    try:
        items = json.loads(items_json)
        item_count = len(items) if isinstance(items, list) else None   # items_json 契约=数组；非数组(串/对象)诚实置 None（不按字符/键数误报）
    except (ValueError, TypeError):
        item_count = None            # 畸形 JSON 不炸卡（诚实置 None）
    return {"request_id": req_id, "item_count": item_count, "created_at": created_at}


def _first_line(md: str) -> str:
    """goal 版本摘要 = 目标正文首个非空行（人机一眼可读；全文在 reasoning 锚点，不在卡里塞全量）。"""
    for line in (md or "").splitlines():
        s = line.strip()
        if s:
            return s
    return ""


def status_card_json(card: Dict[str, Any]) -> str:
    """canonical JSON（sort_keys，防 dict 序差异）——供确定性比对 / 发布落盘。"""
    return json.dumps(card, ensure_ascii=False, sort_keys=True)


class SqliteStatusPublisher:
    """阶段边界原子发布（§4.6.6；M5 CP6.2）：build_status_card → canonical JSON → tmp→os.replace
    覆盖 latest 文件。读者（Mediator/应答器）任意时刻读到的都是**完整**卡（rename 原子，无半完成态）。
    满足 interfaces.StatusPublisher Protocol：publish(cycle_id) -> str（发布文件路径）。

    conn 契约同 build_status_card：**专用**只读用途连接（本类掌控其事务）——传 mode=ro 连接最稳
    （mediator.open_responder_read_conn 同源）。发布失败（磁盘满等）向上抛：卡是派生可重建的，
    但静默丢发布会让人机窗口无声过期——fail loud，重试/降级 = M6 硬化。"""

    def __init__(self, conn, *, policy: Dict[str, Any], goal_body_md: str, out_path: str):
        self.conn = conn
        self.policy = policy
        self.goal_body_md = goal_body_md
        self.out_path = Path(out_path)

    def publish(self, cycle_id: str) -> str:
        card = build_status_card(self.conn, cycle_id=cycle_id, policy=self.policy,
                                 goal_body_md=self.goal_body_md)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.out_path.with_name(self.out_path.name + ".tmp")
        tmp.write_text(status_card_json(card), encoding="utf-8")
        tmp.replace(self.out_path)               # 原子替换：读者不见半写
        return str(self.out_path)
