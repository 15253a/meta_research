"""Durable progress facts which do not have dedicated Appendix-A columns.

``question.visit_count`` is the lifetime anti-greedy counter.  Gate 2 also
needs a distinct *consecutive inconclusive* fact.  The frozen Appendix-A DDL
has no column for it, so each inconclusive terminalization appends a strict
decision event.  Counting only events for the question's current goal version
makes a goal amendment start a fresh streak without erasing lifetime visits.
"""
from __future__ import annotations

import json
from typing import Any, Dict


INCONCLUSIVE_PROTOCOL = "question-inconclusive-v1"
INCONCLUSIVE_DECISION_TYPE = "question_inconclusive"


class QuestionProgressError(ValueError):
    """The append-only question progress ledger is missing or corrupt."""


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QuestionProgressError(f"{field} 须为正整数")
    return value


def load_inconclusive_streak(conn, *, question_id: int) -> Dict[str, Any]:
    """Validate and return the current-goal inconclusive streak.

    A legacy database can have lifetime visits but no events.  It therefore
    starts at streak zero and fails Gate 2 closed until new, auditable
    inconclusive attempts have been recorded.
    """
    question_id = _positive_int(question_id, field="question_id")
    question = conn.execute(
        "SELECT goal_id,goal_ver,visit_count FROM question WHERE id=?",
        (question_id,)).fetchone()
    if question is None:
        raise QuestionProgressError(f"question q{question_id} 不存在")
    goal_id, goal_ver, visit_count = question
    if (isinstance(visit_count, bool) or not isinstance(visit_count, int)
            or visit_count < 0):
        raise QuestionProgressError(
            f"question q{question_id} visit_count 非法")

    rows = conn.execute(
        "SELECT id,cycle_id,payload_json FROM decision WHERE question_id=? "
        "AND actor='orchestrator' AND type=? ORDER BY id",
        (question_id, INCONCLUSIVE_DECISION_TYPE)).fetchall()
    counts: Dict[tuple[int, int], int] = {}
    ids: Dict[tuple[int, int], list[int]] = {}
    seen_cycles = set()
    last_visit = None
    required = {
        "protocol", "question_id", "cycle_id", "goal_id", "goal_ver",
        "visit_count_after", "consecutive_inconclusive",
    }
    for decision_id, cycle_id, payload_raw in rows:
        try:
            payload = json.loads(
                payload_raw,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"非有限 JSON number: {token}")))
        except (json.JSONDecodeError, ValueError) as error:
            raise QuestionProgressError(
                f"question_inconclusive decision {decision_id} payload 损坏"
            ) from error
        if not isinstance(payload, dict) or set(payload) != required:
            raise QuestionProgressError(
                f"question_inconclusive decision {decision_id} 协议字段非法")
        for field in required - {"protocol"}:
            _positive_int(payload[field], field=f"decision {decision_id}.{field}")
        if (payload["protocol"] != INCONCLUSIVE_PROTOCOL
                or payload["question_id"] != question_id
                or payload["cycle_id"] != cycle_id):
            raise QuestionProgressError(
                f"question_inconclusive decision {decision_id} 身份不一致")
        if cycle_id in seen_cycles:
            raise QuestionProgressError(
                f"question q{question_id} cycle c{cycle_id} 重复 inconclusive")
        seen_cycles.add(cycle_id)
        cycle = conn.execute(
            "SELECT goal_id,goal_ver FROM cycle WHERE id=?", (cycle_id,)
        ).fetchone()
        if cycle != (payload["goal_id"], payload["goal_ver"]):
            raise QuestionProgressError(
                f"question_inconclusive decision {decision_id} goal lineage 不一致")
        key = (payload["goal_id"], payload["goal_ver"])
        expected_streak = counts.get(key, 0) + 1
        if payload["consecutive_inconclusive"] != expected_streak:
            raise QuestionProgressError(
                f"question_inconclusive decision {decision_id} streak 不连续")
        if (last_visit is not None
                and payload["visit_count_after"] != last_visit + 1):
            raise QuestionProgressError(
                f"question_inconclusive decision {decision_id} visit 账本不连续")
        counts[key] = expected_streak
        ids.setdefault(key, []).append(decision_id)
        last_visit = payload["visit_count_after"]

    if last_visit is not None and last_visit != visit_count:
        raise QuestionProgressError(
            f"question q{question_id} visit_count 与 inconclusive 账本不一致")
    current = (goal_id, goal_ver)
    return {
        "goal_id": goal_id,
        "goal_ver": goal_ver,
        "visit_count": visit_count,
        "consecutive_inconclusive": counts.get(current, 0),
        "decision_ids": list(ids.get(current, [])),
    }


def append_inconclusive_event(
        conn, *, question_id: int, cycle_id: int) -> Dict[str, Any]:
    """Increment the question and append its matching progress event."""
    question_id = _positive_int(question_id, field="question_id")
    cycle_id = _positive_int(cycle_id, field="cycle_id")
    progress = load_inconclusive_streak(conn, question_id=question_id)
    question = conn.execute(
        "SELECT status,active_cycle FROM question WHERE id=?", (question_id,)
    ).fetchone()
    if question != ("active", cycle_id):
        raise QuestionProgressError(
            f"question q{question_id} 不是 c{cycle_id} 的 active lease")
    cycle = conn.execute(
        "SELECT goal_id,goal_ver FROM cycle WHERE id=?", (cycle_id,)
    ).fetchone()
    if cycle != (progress["goal_id"], progress["goal_ver"]):
        raise QuestionProgressError(
            f"question q{question_id} 与 cycle c{cycle_id} goal lineage 不一致")

    visit_after = progress["visit_count"] + 1
    streak_after = progress["consecutive_inconclusive"] + 1
    changed = conn.execute(
        "UPDATE question SET status='inconclusive',visit_count=? "
        "WHERE id=? AND status='active' AND active_cycle=? AND visit_count=?",
        (visit_after, question_id, cycle_id, progress["visit_count"])).rowcount
    if changed != 1:
        raise QuestionProgressError(
            f"question q{question_id} inconclusive 状态竞态")
    payload = {
        "protocol": INCONCLUSIVE_PROTOCOL,
        "question_id": question_id,
        "cycle_id": cycle_id,
        "goal_id": progress["goal_id"],
        "goal_ver": progress["goal_ver"],
        "visit_count_after": visit_after,
        "consecutive_inconclusive": streak_after,
    }
    decision_id = conn.execute(
        "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
        "VALUES (?,?,'orchestrator',?,?)",
        (cycle_id, question_id, INCONCLUSIVE_DECISION_TYPE,
         json.dumps(payload, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False))).lastrowid
    return {**payload, "decision_id": decision_id}
