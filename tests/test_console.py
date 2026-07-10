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
                                  DirectiveApplicationError, IdempotencyCollisionError,
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
    goal = d.query_one(
        "SELECT m.goal_id,m.goal_ver FROM directive x JOIN interaction_message m "
        "ON m.id=x.source_interaction_message_id WHERE x.id=?", (did,))
    mid = c.ingest.inbound(
        connector="test-console-action", raw_text=directive_action_text(action, did, reason=reason),
        idempotency_key=f"test-{action}-d{did}", goal_id=goal[0], goal_ver=goal[1],
        session_ref=DIRECTIVE_ACTION_SESSION_REF)
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


def test_unimplemented_directive_never_claims_consumed_success(env):
    d, c = env["d"], env["c"]
    r = c.handle_inbound(
        connector="qq", raw_text="设置预算 50", idempotency_key="k-budget",
        goal_id=1, goal_ver=1)
    c.confirm_directive(
        directive_id=r["directive_id"],
        confirm_message_id=_action_message(d, c, r, "confirm"))
    with pytest.raises(DirectiveApplicationError, match="真实状态语义"):
        c.consume_directive(directive_id=r["directive_id"], cycle_id="c1")
    assert d.query_one(
        "SELECT status,consumed_cycle,consumed_decision_id FROM directive WHERE id=?",
        (r["directive_id"],)) == ("pending", None, None)


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


def test_resume_supersedes_only_earlier_pending_pauses(env):
    """外审 SHOULD 回归：resume 只清早于它的 pending pause；晚到的 pause 是新诉求、保留。"""
    d, c = env["d"], env["c"]
    p1 = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="k-p1", goal_id=1, goal_ver=1)
    rs = c.handle_inbound(connector="qq", raw_text="继续", idempotency_key="k-rs", goal_id=1, goal_ver=1)
    p2 = c.handle_inbound(connector="qq", raw_text="暂停 再停一次", idempotency_key="k-p2", goal_id=1, goal_ver=1)
    c.confirm_directive(directive_id=rs["directive_id"],
                        confirm_message_id=_action_message(d, c, rs, "confirm"))
    eff = c.consume_directive(directive_id=rs["directive_id"], cycle_id="c1")
    assert eff.get("superseded_pause") == [p1["directive_id"]]                       # 只清 p1
    assert d.query_one("SELECT status FROM directive WHERE id=?", (p1["directive_id"],))[0] == "superseded"
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
