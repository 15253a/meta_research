"""status_card 构建器（§4.6.6 封闭字段清单）—— 人机控制台的**阶段边界发布派生卡**。

**性质**：派生快照，可从 DB 真相重建 → **不在核心 DDL**（附录 A 无此表，§4.6.2）。故此处是**构建器**，
不建表、不落库；「阶段边界原子发布」的真接入（advance 在 phase 边界调用 + 写 outbox）= M3。M2 交付：
从 DB 真相构建**封闭字段集**、确定性可测。

**封闭字段**（§4.6.6，顺序固定、集合封闭——不多不少）：
  snapshot_cycle · goal(版本摘要) · active_question(问题卡) · cycle_status/route
  · selection(intent + 最近 selection DECISION 摘要) · budget(B(t)/本轮已花/全局剩余)
  · counts(open/inconclusive) · heartbeat_ref · pending_file_request(pending 文件请求摘要)

**M2 暂缺真源的字段**（诚实置 None + 注明，字段仍在封闭集内，M3 接线填）：
  - selection.latest_decision：persist_selection 只更新 cycle.next_*（不写 decision 行）；selection DECISION
    审计行由 advance 落（M3）。M2 的权威 selection 状态 = cycle.next_intent/next_question_id（已渲）。
  - budget.global_remaining：policy 只有单轮 B_max，无全局会话上限（会话级旋钮，非核心 DDL）→ None。
  - heartbeat_ref：heartbeat/outbox 是实现层幂等队列、非核心 DDL（§4.6.2）→ None（M3 outbox 落）。

**纯函数 / 可测**：不调 wall-clock。pending 请求「已等待时长」= 展示时刻 − created_at，由控制台在展示时算；
M2 只给锚点 created_at（不在卡内假造时长——精确换算需全系统时区/格式约定，M3 定）。
"""
from __future__ import annotations

import json
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

        # selection：M2 权威状态取自 cycle.next_*（persist_selection 只更新 cycle、不写 decision 行）。
        # latest_decision（最近 selection DECISION 摘要）审计行由 advance 落（M3）。**此处不写查询**——
        # selection DECISION 无 goal_id，须按「选出本轮问题的那次选择」定作用域（非全局最新），语义属 M3；
        # 先留显式 None，防 M3 误当「已接线且正确」而漏 scope（内审 SHOULD：全局 LIMIT 1 会跨 goal 串卡）。
        selection = {
            "intent": next_intent,
            "next_question_id": f"q{next_q}" if next_q is not None else None,   # M2 权威选择状态（代 DECISION 摘要"选了哪题"）
            "latest_decision": None,   # TODO(M3): advance 落 selection DECISION 后，按 cycle 作用域查其摘要
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
    """canonical JSON（sort_keys，防 dict 序差异）——供确定性比对 / 发布落盘（M3 outbox）。"""
    return json.dumps(card, ensure_ascii=False, sort_keys=True)
