"""CP6.1 · Console 保守分类器 + directive 生命周期（§4.6.2–4.6.4；M5）。

核心验收面（§7.1 M5）：分类负例（unclear 不自动答不产 directive）；润色≠raw（raw 不可变、directive 携
润色稿+source provenance）；未确认硬指令 consume 拒；按时机消费同记 DECISION（actor=human 经 directive，
不 FK message）；软指令系统不从记理由。
"""
from __future__ import annotations

import json

import pytest

import conftest
from orchestrator import database as db
from orchestrator.console import (DIRECTIVE_ACTION_SESSION_REF, Console,
                                  IdempotencyCollisionError,
                                  KeywordClassifier, directive_action_text, sanitize)
from orchestrator.writedaemon import WriteDaemon


@pytest.fixture()
def env():
    daemon = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(daemon.conn)
    return {"d": daemon, "c": Console(daemon)}


def _action_message(d, c, result, action, *, reason=""):
    """模拟结构化控件经单写 ingest 落下的确定性 provenance 消息。"""
    did = result["directive_id"]
    source = d.query_one(
        "SELECT m.goal_id,m.goal_ver,m.connector,m.conversation_id,m.session_ref "
        "FROM directive x JOIN interaction_message m "
        "ON m.id=x.source_interaction_message_id WHERE x.id=?", (did,))
    mid = c.ingest.inbound(
        connector=source[2], raw_text=directive_action_text(action, did, reason=reason),
        idempotency_key=f"test-{action}-d{did}", goal_id=source[0], goal_ver=source[1],
        conversation_id=source[3],
        session_ref=(DIRECTIVE_ACTION_SESSION_REF if source[2] == "console" else None))
    with d.transaction() as conn:
        conn.execute("INSERT INTO interaction_classification(message_id,intent,directive_id) "
                     "VALUES (?,'unclear',NULL)", (mid,))
    return mid


# ============ 分类器（保守铁律）============
def test_classifier_conservative_matrix():
    k = KeywordClassifier()
    assert k.classify({"raw_text": "暂停一下"})["intent"] == "directive"
    assert k.classify({"raw_text": "暂停一下"})["kind"] == "pause"
    assert k.classify({"raw_text": "现在进展如何？"})["intent"] == "query"
    assert k.classify({"raw_text": "备注：这个方向有文献支持"})["intent"] == "note"       # note = DDL 独立 intent
    assert k.classify({"raw_text": "备注：这个方向有文献支持"})["kind"] == "note"
    assert k.classify({"raw_text": "呃"})["intent"] == "unclear"                          # 词表未命中不猜
    assert k.classify({"raw_text": ""})["intent"] == "unclear"


def test_classifier_polite_command_not_query():
    """内审 SHOULD 回归：裸疑问助词不当 query 证据——礼貌式指令进澄清环（unclear），不被静默只读作答。"""
    k = KeywordClassifier()
    assert k.classify({"raw_text": "把这个停掉好吗"})["intent"] == "unclear"   # "停掉"不在词表、"吗"不算 query
    assert k.classify({"raw_text": "现在进展如何"})["intent"] == "query"       # 实义状态词仍走 query


def test_classifier_ascii_word_boundary():
    """内审 SHOULD 回归：ASCII 词中缀不命中（"pin"∈"opinion"），防软指令假阳污染台账。"""
    k = KeywordClassifier()
    assert k.classify({"raw_text": "shipping done."})["intent"] == "unclear"
    assert k.classify({"raw_text": "pin q3 first"})["kind"] == "reprioritize"  # 独立词仍命中
    assert k.classify({"raw_text": "what's your opinion"})["intent"] == "query"  # opinion 不再触发 pin


def test_sanitize_strips_control_chars():
    assert sanitize("a\x00b\x1fc\nd") == "abc\nd"      # 控制字符去、换行留


# ============ unclear 负例（§7.1 M5：不自动答、不产 directive）============
def test_unclear_no_directive_row(env):
    d, c = env["d"], env["c"]
    r = c.handle_inbound(connector="qq", raw_text="呃", idempotency_key="k-u", goal_id=1, goal_ver=1)
    assert r["intent"] == "unclear" and r["directive_id"] is None
    assert d.query_one("SELECT count(*) FROM directive")[0] == 0                     # 无 directive 行（断言原文）
    assert d.query_one("SELECT intent, directive_id FROM interaction_classification "
                       "WHERE message_id=?", (r["message_id"],)) == ("unclear", None)
    assert d.query_one("SELECT count(*) FROM decision WHERE actor='human'")[0] == 0  # 不改状态


# ============ 润色≠raw + provenance 时序 ============
def test_directive_polish_not_raw_and_provenance(env):
    d, c = env["d"], env["c"]
    raw = "帮我  暂停\x01一下"
    r = c.handle_inbound(connector="qq", raw_text=raw, idempotency_key="k-p", goal_id=1, goal_ver=1)
    assert r["intent"] == "directive" and r["needs_confirmation"] is True            # pause 硬指令须确认
    assert d.query_one("SELECT raw_text FROM interaction_message WHERE id=?", (r["message_id"],))[0] == raw  # raw 不可变原样
    dr = d.query_one("SELECT kind, hardness, status, payload_json, source_interaction_message_id "
                     "FROM directive WHERE id=?", (r["directive_id"],))
    assert dr[0] == "pause" and dr[1] == "hard" and dr[2] == "pending"
    assert dr[4] == r["message_id"]                                                  # source provenance 回指
    payload = json.loads(dr[3])
    assert payload["polished"].startswith("[pause]") and "\x01" not in payload["polished"]  # 润色稿 ≠ raw
    assert payload["confirmed"] is False


def test_inbound_idempotent_single_classification(env):
    d, c = env["d"], env["c"]
    r1 = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="k-i", goal_id=1, goal_ver=1)
    r2 = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="k-i", goal_id=1, goal_ver=1)
    assert r1["message_id"] == r2["message_id"] and r2["directive_id"] == r1["directive_id"]
    assert r2["needs_confirmation"] is True      # 外审 r2 SHOULD 回归：重放返回值与首次等价（确认 UI 不漏）
    assert d.query_one("SELECT count(*) FROM interaction_classification")[0] == 1    # 每消息恰一分类
    assert d.query_one("SELECT count(*) FROM directive")[0] == 1                     # 不重复建 directive
    c.confirm_directive(directive_id=r1["directive_id"],
                        confirm_message_id=_action_message(d, c, r1, "confirm"))
    r3 = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="k-i", goal_id=1, goal_ver=1)
    assert r3["needs_confirmation"] is False     # 已确认后重放不再催确认


def test_inbound_collision_is_rejected_before_classification_side_effect(env):
    """message 已落但分类未落的 crash window 中，撞键 body 不能给原 raw 绑定自己的 directive。"""
    d, c = env["d"], env["c"]
    c.ingest.inbound(
        connector="qq", raw_text="原始未分类消息", idempotency_key="k-half",
        goal_id=1, goal_ver=1)
    with pytest.raises(IdempotencyCollisionError, match="其他不可变消息"):
        c.handle_inbound(
            connector="qq", raw_text="暂停", idempotency_key="k-half",
            goal_id=1, goal_ver=1)
    assert d.query_one("SELECT COUNT(*) FROM interaction_classification")[0] == 0
    assert d.query_one("SELECT COUNT(*) FROM directive")[0] == 0


# ============ 硬指令确认门（§7.1 M5：未确认 consume 拒）============
def test_unconfirmed_hard_directive_consume_rejected(env):
    d, c = env["d"], env["c"]
    r = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="k-h", goal_id=1, goal_ver=1)
    with pytest.raises(ValueError, match="未经回显确认"):
        c.consume_directive(directive_id=r["directive_id"], cycle_id="c1")
    assert d.query_one("SELECT status FROM directive WHERE id=?", (r["directive_id"],))[0] == "pending"
    c.confirm_directive(directive_id=r["directive_id"],
                        confirm_message_id=_action_message(d, c, r, "confirm"))
    eff = c.consume_directive(directive_id=r["directive_id"], cycle_id="c1")         # 确认后可消费
    assert eff["kind"] == "pause"
    row = d.query_one("SELECT status, consumed_cycle, consumed_decision_id FROM directive WHERE id=?",
                      (r["directive_id"],))
    assert row[0] == "consumed" and row[1] == 1 and row[2] is not None
    dec = d.query_one("SELECT actor, type, directive_id FROM decision WHERE id=?", (row[2],))
    assert dec == ("human", "directive_pause", r["directive_id"])                    # 同记 DECISION（经 directive 回指）


def test_goal_amend_consumption_records_exact_confirmed_effect(env):
    d, c = env["d"], env["c"]
    r = c.handle_inbound(
        connector="qq", raw_text="修订目标：改成新目标", idempotency_key="k-goal-amend",
        goal_id=1, goal_ver=1)
    c.confirm_directive(
        directive_id=r["directive_id"],
        confirm_message_id=_action_message(d, c, r, "confirm"))
    with d.transaction() as conn:
        conn.execute("UPDATE cycle SET route='goal_amend' WHERE id=1")
    effect = c.consume_directive(directive_id=r["directive_id"], cycle_id="c1")
    assert effect == {
        "kind": "goal_amend", "new_goal_text": "新目标", "predicate_json": {},
        "rationale_md": "用户明确修订研究目标", "source_goal_ver": 1,
        "target_goal_ver": 2, "applies_to_reasoning_cycle": "c1"}
    assert d.query_one(
        "SELECT status,consumed_cycle,consumed_decision_id FROM directive WHERE id=?",
        (r["directive_id"],))[:2] == ("consumed", 1)
    assert d.query_one(
        "SELECT actor,type FROM decision WHERE directive_id=? ORDER BY id DESC LIMIT 1",
        (r["directive_id"],)) == ("human", "directive_goal_amend")
    assert d.query_one("SELECT count(*) FROM goal")[0] == 1  # 应用须等 reasoning 原子收尾


def test_goal_amend_classifier_structured_json_and_rejects_unknown():
    k = KeywordClassifier()
    parsed = k.classify({"raw_text":
        'goal amend {"new_goal_text":"新目标","predicate_json":{"metric":"acc"},"rationale_md":"收紧"}'})
    assert parsed["structured"] == {
        "new_goal_text": "新目标", "predicate_json": {"metric": "acc"}, "rationale_md": "收紧"}
    bad = k.classify({"raw_text": '修订目标 {"new_goal_text":"x","unknown":1}'})
    assert "不允许字段" in bad["structured"]["parse_error"]
    braces = k.classify({"raw_text": "修订目标：研究集合 A={x|x>0} 的稳健规律"})
    assert braces["structured"]["new_goal_text"] == "研究集合 A={x|x>0} 的稳健规律"


def test_goal_amend_stale_confirmation_becomes_superseded(env):
    d, c = env["d"], env["c"]
    r = c.handle_inbound(
        connector="qq", raw_text="修订目标：旧页面里的改版", idempotency_key="stale-amend",
        goal_id=1, goal_ver=1)
    with d.transaction() as conn:
        conn.execute(
            "INSERT INTO goal(id,version,text,predicate_json,previous_version) "
            "VALUES (1,2,'已由其他改版生效','{}',1)")
    c.confirm_directive(
        directive_id=r["directive_id"],
        confirm_message_id=_action_message(d, c, r, "confirm"))
    status, payload_raw = d.query_one(
        "SELECT status,payload_json FROM directive WHERE id=?", (r["directive_id"],))
    assert status == "superseded"
    assert json.loads(payload_raw)["superseded_reason"] == "source_goal_not_current"


def test_latest_confirmed_goal_amend_supersedes_older_pending(env):
    d, c = env["d"], env["c"]
    first = c.handle_inbound(
        connector="qq", raw_text="修订目标：第一版", idempotency_key="amend-first",
        goal_id=1, goal_ver=1)
    second = c.handle_inbound(
        connector="qq", raw_text="修订目标：第二版", idempotency_key="amend-second",
        goal_id=1, goal_ver=1)
    c.confirm_directive(
        directive_id=first["directive_id"],
        confirm_message_id=_action_message(d, c, first, "confirm"))
    c.confirm_directive(
        directive_id=second["directive_id"],
        confirm_message_id=_action_message(d, c, second, "confirm"))
    assert d.query(
        "SELECT status FROM directive WHERE id IN (?,?) ORDER BY id",
        (first["directive_id"], second["directive_id"])) == [("superseded",), ("pending",)]


def test_malformed_confirmed_goal_amend_is_rejected_without_superseding_valid(env):
    """不可执行的新修订不是 effective amendment，不能抹掉较早的有效用户意图。"""
    d, c = env["d"], env["c"]
    valid = c.handle_inbound(
        connector="qq", raw_text="修订目标：有效目标", idempotency_key="valid-amend",
        goal_id=1, goal_ver=1)
    c.confirm_directive(
        directive_id=valid["directive_id"],
        confirm_message_id=_action_message(d, c, valid, "confirm"))
    malformed = c.handle_inbound(
        connector="qq", raw_text='修订目标 {"new_goal_text":"坏目标","unknown":1}',
        idempotency_key="malformed-amend", goal_id=1, goal_ver=1)
    c.confirm_directive(
        directive_id=malformed["directive_id"],
        confirm_message_id=_action_message(d, c, malformed, "confirm"))

    assert d.query_one(
        "SELECT status FROM directive WHERE id=?", (valid["directive_id"],))[0] == "pending"
    status, payload_raw = d.query_one(
        "SELECT status,payload_json FROM directive WHERE id=?", (malformed["directive_id"],))
    assert status == "rejected"
    payload = json.loads(payload_raw)
    assert payload["confirmed"] is True and payload["confirmation_message_id"]
    assert payload["rejection_kind"] == "application_unavailable"
    assert d.query_one(
        "SELECT actor,type FROM decision WHERE directive_id=?",
        (malformed["directive_id"],)) == ("orchestrator", "directive_application_rejected")


def test_goal_amend_current_version_and_supersession_are_goal_id_scoped(env):
    """其它 goal 的更高 version / 更新 directive 不得把本 goal 的合法修订判 stale 或覆盖。"""
    d, c = env["d"], env["c"]
    with d.transaction() as conn:
        conn.execute(
            "INSERT INTO goal(id,version,text,predicate_json) VALUES (2,2,'另一个目标 v2','{}')")
    goal1 = c.handle_inbound(
        connector="qq", raw_text="修订目标：目标一新版", idempotency_key="g1-amend",
        goal_id=1, goal_ver=1)
    goal2 = c.handle_inbound(
        connector="qq", raw_text="修订目标：目标二新版", idempotency_key="g2-amend",
        goal_id=2, goal_ver=2)
    # goal2 directive id 更新且先确认；随后确认 goal1 时不能跨 goal 看见/覆盖它。
    c.confirm_directive(
        directive_id=goal2["directive_id"],
        confirm_message_id=_action_message(d, c, goal2, "confirm"))
    c.confirm_directive(
        directive_id=goal1["directive_id"],
        confirm_message_id=_action_message(d, c, goal1, "confirm"))
    c.supersede_stale_goal_amends()
    assert d.query(
        "SELECT status FROM directive WHERE id IN (?,?) ORDER BY id",
        (goal1["directive_id"], goal2["directive_id"])) == [("pending",), ("pending",)]

    with d.transaction() as conn:
        conn.execute("UPDATE cycle SET route='goal_amend' WHERE id=1")
    effect = c.consume_directive(directive_id=goal1["directive_id"], cycle_id="c1")
    assert effect["source_goal_ver"] == 1 and effect["target_goal_ver"] == 2
    assert effect["new_goal_text"] == "目标一新版"


def test_reject_by_user_no_decision(env):
    d, c = env["d"], env["c"]
    r = c.handle_inbound(connector="qq", raw_text="剪枝 q1", idempotency_key="k-r", goal_id=1, goal_ver=1)
    before = d.query_one("SELECT count(*) FROM decision")[0]
    reason = "用户否掉润色稿"
    c.reject_directive(directive_id=r["directive_id"], reason=reason,
                       reject_message_id=_action_message(d, c, r, "reject", reason=reason))
    st, pj = d.query_one("SELECT status, payload_json FROM directive WHERE id=?", (r["directive_id"],))
    assert st == "rejected"
    assert json.loads(pj)["rejection_reason"] == "用户否掉润色稿"                    # 理由入 payload 供审计
    assert d.query_one("SELECT count(*) FROM decision")[0] == before                 # 用户否决不写 decision（P1）


def test_reject_reason_is_bound_to_action_provenance(env):
    """拒绝理由是控制动作的一部分；同一消息不得被换一个理由重放。"""
    d, c = env["d"], env["c"]
    r = c.handle_inbound(connector="qq", raw_text="剪枝 q1", idempotency_key="k-r-reason",
                         goal_id=1, goal_ver=1)
    mid = _action_message(d, c, r, "reject", reason="理由甲")
    with pytest.raises(ValueError, match="原文不符"):
        c.reject_directive(directive_id=r["directive_id"], reason="理由乙", reject_message_id=mid)
    assert d.query_one("SELECT status FROM directive WHERE id=?", (r["directive_id"],)) == ("pending",)


def test_directive_action_provenance_rejects_arbitrary_or_cross_goal_messages(env):
    """最终迁移事务不能信任任意既有 message id；raw/classification/goal 均须匹配控件动作。"""
    d, c = env["d"], env["c"]
    r = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="k-prov",
                         goal_id=1, goal_ver=1)
    with pytest.raises(ValueError, match="原文不符"):
        c.confirm_directive(directive_id=r["directive_id"], confirm_message_id=r["message_id"])

    did = r["directive_id"]
    wrong_class = c.ingest.inbound(
        connector="qq", raw_text=directive_action_text("confirm", did),
        idempotency_key="k-prov-class", goal_id=1, goal_ver=1,
        session_ref=DIRECTIVE_ACTION_SESSION_REF)
    with d.transaction() as conn:
        conn.execute("INSERT INTO interaction_classification(message_id,intent,directive_id) "
                     "VALUES (?,'query',NULL)", (wrong_class,))
    with pytest.raises(ValueError, match="unclear"):
        c.confirm_directive(directive_id=did, confirm_message_id=wrong_class)

    wrong_goal = c.ingest.inbound(
        connector="qq", raw_text=directive_action_text("confirm", did),
        idempotency_key="k-prov-goal", goal_id=None, goal_ver=None,
        session_ref=DIRECTIVE_ACTION_SESSION_REF)
    with d.transaction() as conn:
        conn.execute("INSERT INTO interaction_classification(message_id,intent,directive_id) "
                     "VALUES (?,'unclear',NULL)", (wrong_goal,))
    with pytest.raises(ValueError, match="source goal"):
        c.confirm_directive(directive_id=did, confirm_message_id=wrong_goal)
    with pytest.raises(ValueError, match="reject_message_id"):
        c.reject_directive(directive_id=did, reason="拒绝但无 provenance")
    assert d.query_one("SELECT status FROM directive WHERE id=?", (did,))[0] == "pending"


def test_soft_directive_declined_with_decision(env):
    d, c = env["d"], env["c"]
    r = c.handle_inbound(connector="qq", raw_text="注入问题：试试 CNN 基线", idempotency_key="k-s",
                         goal_id=1, goal_ver=1)
    assert r["needs_confirmation"] is False                                          # 软指令免确认
    c.reject_directive(directive_id=r["directive_id"], reason="与当前目标谓词无关", by_decision=True, cycle_id="c1")
    dec = d.query_one("SELECT actor, type, payload_json FROM decision ORDER BY id DESC LIMIT 1")
    assert dec[0] == "orchestrator" and dec[1] == "soft_directive_declined"
    assert "无关" in dec[2]                                                          # 不从须记理由（§4.6.4）


# ============ 消费效果（按时机 + 最小效果）============
def test_inject_question_effect(env):
    d, c = env["d"], env["c"]
    r = c.handle_inbound(connector="qq", raw_text="注入问题：CNN 能到 0.95 吗", idempotency_key="k-q",
                         goal_id=1, goal_ver=1)
    assert r["directive_id"] in c.pending_directives("reasoning_start")              # 时机=下一轮 reasoning 始
    eff = c.consume_directive(directive_id=r["directive_id"], cycle_id="c1")
    q = d.query_one("SELECT text, status, source FROM question WHERE id=?", (int(eff["question_id"][1:]),))
    assert q[1] == "open" and q[2] == "human" and "CNN" in q[0]


def test_prune_branch_effect_and_deadend_decision(env):
    d, c = env["d"], env["c"]
    with d.transaction() as conn:
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                     "VALUES (2,1,1,1,'q2 可剪','open','agent')")
    r = c.handle_inbound(connector="qq", raw_text="剪枝这条", idempotency_key="k-pr", goal_id=1, goal_ver=1)
    c.confirm_directive(directive_id=r["directive_id"],
                        confirm_message_id=_action_message(d, c, r, "confirm"))
    with d.transaction() as conn:   # 润色/确认阶段补齐目标（真流程中介补；测试直写 payload）
        p = json.loads(d.query_one("SELECT payload_json FROM directive WHERE id=?", (r["directive_id"],))[0])
        p["question_id"] = "q2"
        conn.execute("UPDATE directive SET payload_json=? WHERE id=?", (json.dumps(p), r["directive_id"]))
    c.consume_directive(directive_id=r["directive_id"], cycle_id="c1")
    assert d.query_one("SELECT status FROM question WHERE id=2")[0] == "dead_end"
    assert d.query_one("SELECT count(*) FROM decision WHERE type='prune_branch' AND question_id=2")[0] == 1
    # 外审 SHOULD 回归：一次消费恰一条人类决策——prune 决策即消费决策，无重复 directive_prune_branch 行
    assert d.query_one("SELECT count(*) FROM decision WHERE directive_id=?", (r["directive_id"],))[0] == 1
    cd = d.query_one("SELECT consumed_decision_id FROM directive WHERE id=?", (r["directive_id"],))[0]
    assert d.query_one("SELECT type FROM decision WHERE id=?", (cd,))[0] == "prune_branch"


def test_pause_blocks_from_consume_until_resume_consumed(env):
    """外审 BLOCKER 回归：pause 状态模型——阻断 = 最近被消费的 pause/resume 是 pause。
    pause 消费（记 DECISION）即进暂停态并**持续阻断**；resume 消费才解除。pending 不阻断。"""
    d, c = env["d"], env["c"]
    r = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="k-b", goal_id=1, goal_ver=1)
    c.confirm_directive(directive_id=r["directive_id"],
                        confirm_message_id=_action_message(d, c, r, "confirm"))
    assert c.has_blocking_pause() is False                                           # pending（已确认）不阻断
    c.consume_directive(directive_id=r["directive_id"], cycle_id="c1")
    assert c.has_blocking_pause() is True                                            # 消费后进入并保持暂停态
    r2 = c.handle_inbound(connector="qq", raw_text="继续", idempotency_key="k-c", goal_id=1, goal_ver=1)
    c.confirm_directive(directive_id=r2["directive_id"],
                        confirm_message_id=_action_message(d, c, r2, "confirm"))
    assert c.has_blocking_pause() is True                                            # resume 未消费仍阻断
    c.consume_directive(directive_id=r2["directive_id"], cycle_id="c1")
    assert c.has_blocking_pause() is False                                           # resume 消费解除


def test_running_continue_is_ack_not_resume_and_keeps_pending_pauses(env):
    """reference 特例：尚未消费的 pause 不等于已暂停；继续只 ACK、零状态效果。"""
    d, c = env["d"], env["c"]
    p1 = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="k-p1", goal_id=1, goal_ver=1)
    rs = c.handle_inbound(connector="qq", raw_text="继续", idempotency_key="k-rs", goal_id=1, goal_ver=1)
    p2 = c.handle_inbound(connector="qq", raw_text="暂停 再停一次", idempotency_key="k-p2", goal_id=1, goal_ver=1)
    assert rs["intent"] == "query" and rs["directive_id"] is None
    assert rs["special"] == "continue_running"
    assert d.query_one(
        "SELECT count(*) FROM interaction_reply WHERE message_id=?", (rs["message_id"],))[0] == 1
    assert d.query_one("SELECT status FROM directive WHERE id=?", (p1["directive_id"],))[0] == "pending"
    assert d.query_one("SELECT status FROM directive WHERE id=?", (p2["directive_id"],))[0] == "pending"  # p2 保留


def test_pending_directives_excludes_unconfirmed_hard(env):
    """外审 SHOULD 回归：待消费队列只出软指令/已确认硬指令——未确认硬指令进队只会稳定撞 consume 拒。"""
    d, c = env["d"], env["c"]
    hard = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="k-hq", goal_id=1, goal_ver=1)
    assert hard["directive_id"] not in c.pending_directives("immediate")             # 未确认硬不进队
    c.confirm_directive(directive_id=hard["directive_id"],
                        confirm_message_id=_action_message(d, c, hard, "confirm"))
    assert hard["directive_id"] in c.pending_directives("immediate")                 # 确认后进队


def test_abort_cycle_effect(env):
    d, c = env["d"], env["c"]   # seed cycle1 是 reasoning 非终态 → abort 对象
    with d.transaction() as conn:
        conn.execute(
            "INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source,visit_count) "
            "VALUES (2,1,1,1,'abort 后须恢复调度','active','agent',3)")
        conn.execute("UPDATE cycle SET active_question_id=2 WHERE id=1")
    r = c.handle_inbound(connector="qq", raw_text="abort 本轮", idempotency_key="k-a", goal_id=1, goal_ver=1)
    c.confirm_directive(directive_id=r["directive_id"],
                        confirm_message_id=_action_message(d, c, r, "confirm"))
    eff = c.consume_directive(directive_id=r["directive_id"], cycle_id="c1")
    assert eff == {"kind": "abort_cycle", "released_question": "q2", "aborted_cycle": "c1"}
    assert d.query_one("SELECT status,active_question_id FROM cycle WHERE id=1") == ("aborted", None)
    assert d.query_one("SELECT status,visit_count FROM question WHERE id=2") == ("open", 3)


def test_note_classification_null_directive_id_but_directive_exists(env):
    """note = DDL 独立 intent：分类行 directive_id 必空（CHECK），directive(kind=note,soft) 仍建，
    provenance 走 directive.source_interaction_message_id；重放经 source 找回 id。"""
    d, c = env["d"], env["c"]
    r = c.handle_inbound(connector="qq", raw_text="备注：好方向", idempotency_key="k-n", goal_id=1, goal_ver=1)
    assert r["intent"] == "note" and r["directive_id"] is not None
    assert d.query_one("SELECT intent, directive_id FROM interaction_classification WHERE message_id=?",
                       (r["message_id"],)) == ("note", None)                         # CHECK：非 directive 意图 id 必空
    assert d.query_one("SELECT kind, hardness, source_interaction_message_id FROM directive WHERE id=?",
                       (r["directive_id"],)) == ("note", "soft", r["message_id"])
    r2 = c.handle_inbound(connector="qq", raw_text="备注：好方向", idempotency_key="k-n", goal_id=1, goal_ver=1)
    assert r2["directive_id"] == r["directive_id"]                                   # 重放经 source 回指找回


def test_consume_twice_rejected(env):
    d, c = env["d"], env["c"]
    r = c.handle_inbound(connector="qq", raw_text="备注：好方向", idempotency_key="k-n", goal_id=1, goal_ver=1)
    c.consume_directive(directive_id=r["directive_id"], cycle_id="c1")
    with pytest.raises(ValueError, match="非 pending"):
        c.consume_directive(directive_id=r["directive_id"], cycle_id="c1")
