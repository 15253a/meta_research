"""CP2.2 · SQLiteStateStore（M1b：状态机落 SQLite + decompose 单事务原子性 + kill-9 无半写）。

语义与 M0 InMemoryStateStore 等价（见 statestore.py），此处只验 SQLite 真实现的正确性与原子性。
`_force_answer` 用 literature 证据把某问题合法关成 answered（模拟 gate_close_question，本检查点不实现它），
供 dep 解算 / applicability 用例。
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from orchestrator import database as db
from orchestrator.question_progress import load_inconclusive_streak
from orchestrator.statestore_sqlite import SQLiteStateStore, _cnum, _qnum, _anum
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent

TEST_POLICY = {
    "policy_version": "test-v1",
    "tree_guard": {"max_decompose_depth": 3, "max_children_per_node": 3, "max_open_questions": 20},
    "question_guard": {"max_inconclusive_per_question": 2},
    "answer_review": {"max_reviews_per_cycle": 2},
    "goal_amend": {"max_spawn_from_goal_amend": 2, "max_closed_revalidate_per_cycle": 3},
}


def _new_store(path=":memory:"):
    s = SQLiteStateStore(WriteDaemon(db.connect(path)), TEST_POLICY)
    return s


@pytest.fixture()
def store():
    s = _new_store()
    s.create_goal(text="root goal", predicate_json={"kind": "x"})
    return s


def _bootstrap_root(s) -> str:
    c = s.open_or_resume_cycle()
    s.set_route(c.cycle_id, "bootstrap")
    s.apply_tree_ops(c.cycle_id, [{"op": "create_root", "text": "root Q", "local_key": "root"}])
    s.mark_cycle_done(c.cycle_id)
    return s.list_schedulable_questions()[0]["question_id"]


def _decompose(s, parent_qid, n=2):
    c = s.open_or_resume_cycle()
    s.set_route(c.cycle_id, "decompose")
    s.activate_question(parent_qid)
    s.apply_tree_ops(c.cycle_id, [{"op": "add_children", "parent_question_id": parent_qid,
                                   "children": [{"local_key": f"ch{i}", "text": f"child {i}"} for i in range(n)]}])
    return c.cycle_id


def _force_answer(s, qid, cycle_id, verdict="answered"):
    """test-only：用 literature 证据把 qid 合法关闭（模拟 gate_close_question）。"""
    qi = _qnum(qid)
    with s.daemon.transaction() as conn:
        gver = conn.execute("SELECT goal_ver FROM question WHERE id=?", (qi,)).fetchone()[0]
        aid = conn.execute("INSERT INTO answer(question_id,goal_id,goal_ver,cycle_id,verdict,answer_md) "
                           "VALUES (?,1,?,?,?,'md')", (qi, gver, _cnum(cycle_id), verdict)).lastrowid
        conn.execute("INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,literature_ref,claim_md) "
                     "VALUES (?,?,1,?,'literature','ref','c')", (aid, qi, gver))
        conn.execute("UPDATE question SET status=? WHERE id=?", (verdict, qi))
        conn.execute("UPDATE cycle SET active_question_id=NULL WHERE id=? AND active_question_id=?",
                     (_cnum(cycle_id), qi))
    return f"a{aid}"


def _start_goal_amend(s, *, text="g2", predicate=None, rationale="r"):
    """Install the exact consumed human authority required by production StateStore."""
    c = s.open_or_resume_cycle()
    ci = _cnum(c.cycle_id)
    with s.daemon.transaction() as conn:
        current_ver, current_predicate = conn.execute(
            "SELECT version,predicate_json FROM goal WHERE id=1 ORDER BY version DESC LIMIT 1").fetchone()
        effective_predicate = (json.loads(current_predicate) if predicate is None else predicate)
        serial = conn.execute("SELECT coalesce(max(id),0)+1 FROM directive").fetchone()[0]
        mid = conn.execute(
            "INSERT INTO interaction_message(connector,goal_id,goal_ver,cycle_id,raw_text,raw_hash,idempotency_key) "
            "VALUES ('test',1,?,?,?,'sha256:test',?)",
            (current_ver, ci, f"amend {text}", f"goal-amend-{serial}")).lastrowid
        payload = {"confirmed": True, "new_goal_text": text,
                   "predicate_json": effective_predicate, "rationale_md": rationale,
                   "polished": "test goal amendment"}
        did = conn.execute(
            "INSERT INTO directive(kind,hardness,status,consume_at,payload_json,created_cycle,"
            "source_interaction_message_id) VALUES ('goal_amend','hard','pending','reasoning_start',?,?,?)",
            (json.dumps(payload, ensure_ascii=False), ci, mid)).lastrowid
        conn.execute(
            "INSERT INTO interaction_classification(message_id,intent,directive_id) "
            "VALUES (?,'directive',?)", (mid, did))
        conn.execute(
            "INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
            "VALUES (?,?,'orchestrator','goal_amend_routed','{\"route\":\"goal_amend\"}')",
            (ci, did))
        effect = {"kind": "goal_amend", "new_goal_text": text,
                  "predicate_json": effective_predicate, "rationale_md": rationale,
                  "source_goal_ver": current_ver, "target_goal_ver": current_ver + 1,
                  "applies_to_reasoning_cycle": c.cycle_id}
        decision = conn.execute(
            "INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
            "VALUES (?,?,'human','directive_goal_amend',?)",
            (ci, did, json.dumps({"effect": effect}, ensure_ascii=False))).lastrowid
        conn.execute(
            "UPDATE directive SET status='consumed',consumed_cycle=?,consumed_decision_id=? WHERE id=?",
            (ci, decision, did))
        conn.execute("UPDATE cycle SET route='goal_amend' WHERE id=?", (ci,))
    op = {"op": "amend_goal", "new_goal_text": text,
          "predicate_json": effective_predicate, "rationale_md": rationale}
    return c, op, did


def _store_with(**tree_guard):
    pol = {"policy_version": "t",
           "tree_guard": {"max_decompose_depth": 3, "max_children_per_node": 3, "max_open_questions": 20, **tree_guard},
           "question_guard": {"max_inconclusive_per_question": 2},
           "answer_review": {"max_reviews_per_cycle": 2},
           "goal_amend": {"max_spawn_from_goal_amend": 2, "max_closed_revalidate_per_cycle": 3}}
    s = SQLiteStateStore(WriteDaemon(db.connect(":memory:")), pol)
    s.create_goal(text="g", predicate_json={})
    return s


# ============ 类型前缀 id / active_question 落库（codex BLOCKER 修复回归） ============
def test_typed_id_rejects_wrong_prefix(store):
    root = _bootstrap_root(store)
    with pytest.raises(ValueError, match="前缀"):
        store.activate_question("c1")          # cycle id 处于 question 位 → 拒（非静默命中 q1）
    with pytest.raises(ValueError, match="前缀"):
        store.set_route("q1", "attack")        # question id 处于 cycle 位 → 拒


def test_active_question_id_persisted_and_cleared(store):
    root = _bootstrap_root(store)
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    store.activate_question(root)
    assert store.open_or_resume_cycle().question_id == root      # 落库 → resume（含崩溃重启）可见本轮攻坚目标
    store.mark_inconclusive(root)
    assert store.open_or_resume_cycle().question_id is None      # inconclusive 释放 → 清空


def test_activate_question_cannot_overwrite_active_lease(store):
    root = _bootstrap_root(store)
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    store.activate_question(root)
    with store.daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO question(goal_id,goal_ver,born_goal_ver,text,status,source) "
            "VALUES (1,1,1,'other','open','agent')")
    with pytest.raises(ValueError, match="不得覆盖激活租约"):
        store.activate_question("q2")
    assert store.cycle(c.cycle_id).question_id == root


def test_mark_cycle_done_rejects_active_question(store):
    root = _bootstrap_root(store)
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    store.activate_question(root)
    with pytest.raises(RuntimeError, match="仍持有 active_question_id"):
        store.mark_cycle_done(c.cycle_id)
    assert store.cycle(c.cycle_id).status != "done"


def test_max_open_questions_accounts_for_released_parent():
    s = _store_with(max_children_per_node=5, max_open_questions=2)
    root = _bootstrap_root(s)                                    # root open（1）
    c = s.open_or_resume_cycle(); s.set_route(c.cycle_id, "decompose"); s.activate_question(root)  # root active（0 open）
    with pytest.raises(ValueError, match="max_open_questions"):  # 2 子 + 释放的父 = 3 open > 2（旧代码漏算父 +1 会误放行）
        s.apply_tree_ops(c.cycle_id, [{"op": "add_children", "parent_question_id": root,
                                       "children": [{"local_key": "a", "text": "a"}, {"local_key": "b", "text": "b"}]}])


def test_max_children_per_node_cumulative(store):
    """max_children_per_node 是节点**累计**上限：两次分解累计超限被拒（非仅单次 op）。"""
    root = _bootstrap_root(store)                               # cap=3
    cid = _decompose(store, root, n=2)                          # 累计 2 子
    for ch in store.daemon.query("SELECT id FROM question WHERE parent_id=?", (_qnum(root),)):
        _force_answer(store, f"q{ch[0]}", cid)
    store.resolve_deps(); store.mark_cycle_done(cid)
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "decompose"); store.activate_question(root)
    with pytest.raises(ValueError, match="max_children_per_node"):   # 再 2 子 → 累计 4 > 3
        store.apply_tree_ops(c.cycle_id, [{"op": "add_children", "parent_question_id": root,
                                           "children": [{"local_key": "c", "text": "c"}, {"local_key": "d", "text": "d"}]}])


def test_max_closed_revalidate_cumulative(store):
    """max_closed_revalidate_per_cycle 是本轮**累计**上限：同轮多次 seed 累计超限被拒。"""
    root = _bootstrap_root(store)                              # cap=3
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    for i in range(4):   # spawn+close 4 个 answer
        store.apply_tree_ops(c.cycle_id, [{"op": "spawn_question", "kind": "followup",
                                           "parent_question_id": root, "text": f"f{i}", "local_key": f"f{i}"}])
    aids = [_force_answer(store, f"q{r[0]}", c.cycle_id)
            for r in store.daemon.query("SELECT id FROM question WHERE parent_id=?", (_qnum(root),))]
    store.mark_cycle_done(c.cycle_id)
    g, amend, _ = _start_goal_amend(store)
    store.apply_tree_ops(g.cycle_id, [amend,
        {"op": "seed_applicability_audit", "answer_ids": aids[:2], "rationale_md": "r"}])
    with pytest.raises(ValueError, match="max_closed_revalidate_per_cycle"):   # 累计 2+2=4 > 3
        store.apply_tree_ops(g.cycle_id, [{"op": "seed_applicability_audit", "answer_ids": aids[2:], "rationale_md": "r"}])


def test_revalidate_cumulative_across_cycles_no_bypass(store):
    """codex 第2轮 BLOCKER 回归：re-seed 旧轮同版 applicability 行会把 audit_cycle 迁到本轮、计入本轮预算，
    防「分批 re-seed 旧行」绕过 per-cycle 上限（无 audit_cycle 更新时可无限绕过）。"""
    root = _bootstrap_root(store)                              # cap=3；答案生于 v1
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    for i in range(4):
        store.apply_tree_ops(c.cycle_id, [{"op": "spawn_question", "kind": "followup",
                                           "parent_question_id": root, "text": f"f{i}", "local_key": f"f{i}"}])
    aids = [_force_answer(store, f"q{r[0]}", c.cycle_id)
            for r in store.daemon.query("SELECT id FROM question WHERE parent_id=?", (_qnum(root),))]
    store.mark_cycle_done(c.cycle_id)
    a, amend_a, _ = _start_goal_amend(store, text="g2")
    store.apply_tree_ops(a.cycle_id, [amend_a,
        {"op": "seed_applicability_audit", "answer_ids": aids[:2], "rationale_md": "r"}])
    store.mark_cycle_done(a.cycle_id)
    b, amend_b, _ = _start_goal_amend(store, text="g3")
    store.apply_tree_ops(b.cycle_id, [amend_b,
        {"op": "seed_applicability_audit", "answer_ids": aids[:2], "rationale_md": "r"}])
    with pytest.raises(ValueError, match="max_closed_revalidate_per_cycle"):      # 再 seed 另 2 → 本轮累计 4 > 3
        store.apply_tree_ops(b.cycle_id, [{"op": "seed_applicability_audit", "answer_ids": aids[2:], "rationale_md": "r"}])


# ============ goal / cycle ============
def test_create_goal_once(store):
    assert store.daemon.query_one("SELECT text FROM goal WHERE id=1")[0] == "root goal"
    with pytest.raises(ValueError, match="goal 已存在"):
        store.create_goal(text="x", predicate_json={})


def test_open_resume_cycle(store):
    c1 = store.open_or_resume_cycle()
    assert c1.status == "created" and c1.cycle_id == "c1"
    assert store.open_or_resume_cycle().cycle_id == "c1"        # 未终态 → resume 同一轮
    store.mark_cycle_done(c1.cycle_id)
    assert store.open_or_resume_cycle().cycle_id == "c2"        # 终态 → 开新轮


def test_set_route_validation(store):
    c = store.open_or_resume_cycle()
    with pytest.raises(ValueError, match="7 形态"):
        store.set_route(c.cycle_id, "nonsense")
    store.set_route(c.cycle_id, "bootstrap")
    store.mark_cycle_done(c.cycle_id)
    with pytest.raises(ValueError, match="已终态"):
        store.set_route(c.cycle_id, "attack")


def test_persist_selection_stale_cycle_fails_loud(store):
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    with store.daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO goal(id,version,text,predicate_json,previous_version) "
            "VALUES (1,2,'v2','{}',1)")
    from orchestrator.interfaces import Selection
    with pytest.raises(RuntimeError, match="非 current"):
        store.persist_selection(
            c.cycle_id,
            Selection(next_question_id=None, next_intent="terminate", scores=[]))


def test_revalidate_parent_must_have_closed_answer(store):
    root = _bootstrap_root(store)
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    with pytest.raises(ValueError, match="带 answer"):
        store.apply_tree_ops(c.cycle_id, [{
            "op": "spawn_question", "kind": "revalidate",
            "parent_question_id": root, "text": "invalid review"}])


# ============ bootstrap / decompose ============
def test_bootstrap_create_root(store):
    root = _bootstrap_root(store)
    r = store.daemon.query_one("SELECT status,parent_id,source FROM question WHERE id=?", (_qnum(root),))
    assert r == ("open", None, "agent")
    assert store.daemon.query_one("SELECT count(*) FROM decision WHERE type='create_root'")[0] == 1


def test_create_root_only_in_bootstrap(store):
    c = store.open_or_resume_cycle()
    store.set_route(c.cycle_id, "attack")
    with pytest.raises(ValueError, match="create_root 仅限 bootstrap"):
        store.apply_tree_ops(c.cycle_id, [{"op": "create_root", "text": "x"}])


def test_decompose_releases_parent_writes_children_and_deps(store):
    root = _bootstrap_root(store)
    _decompose(store, root, n=2)
    # 父 active→open 释放、decompose_count+1
    assert store.daemon.query_one("SELECT status,decompose_count FROM question WHERE id=?", (_qnum(root),)) == ("open", 1)
    kids = store.daemon.query("SELECT id FROM question WHERE parent_id=?", (_qnum(root),))
    assert len(kids) == 2
    # 逐子 pending dep（父依赖每个子）
    deps = store.daemon.query("SELECT depends_on_question_id,status FROM question_dep WHERE question_id=?", (_qnum(root),))
    assert len(deps) == 2 and all(d[1] == "pending" for d in deps)
    assert {d[0] for d in deps} == {k[0] for k in kids}
    assert store.daemon.query_one("SELECT count(*) FROM decision WHERE type='decompose'")[0] == 1


def test_decompose_atomic_rollback_on_guard_violation(store):
    """add_children 超 max_children_per_node → 整批回滚：父仍 active、无子、无 dep（原子性）。"""
    root = _bootstrap_root(store)
    c = store.open_or_resume_cycle()
    store.set_route(c.cycle_id, "decompose")
    store.activate_question(root)
    too_many = [{"local_key": f"ch{i}", "text": f"c{i}"} for i in range(4)]   # > max 3
    with pytest.raises(ValueError, match="max_children_per_node"):
        store.apply_tree_ops(c.cycle_id, [{"op": "add_children", "parent_question_id": root, "children": too_many}])
    assert store.daemon.query_one("SELECT status FROM question WHERE id=?", (_qnum(root),))[0] == "active"  # 未释放
    assert store.daemon.query_one("SELECT count(*) FROM question WHERE parent_id=?", (_qnum(root),))[0] == 0
    assert store.daemon.query_one("SELECT count(*) FROM question_dep")[0] == 0


def test_add_children_requires_active_parent(store):
    root = _bootstrap_root(store)   # root 是 open、非 active
    c = store.open_or_resume_cycle()
    store.set_route(c.cycle_id, "decompose")
    with pytest.raises(ValueError, match="父问题须为 active"):
        store.apply_tree_ops(c.cycle_id, [{"op": "add_children", "parent_question_id": root,
                                           "children": [{"local_key": "a", "text": "a"}]}])


# ============ 调度可见性 / dep ============
def test_pending_question_dep_blocks_scheduling(store):
    root = _bootstrap_root(store)
    _decompose(store, root)                       # root 现 open 但有 2 pending dep
    assert store.is_schedulable(root) is False
    assert root not in [q["question_id"] for q in store.list_schedulable_questions()]


def test_resolve_deps_question_and_baseline(store):
    root = _bootstrap_root(store)
    cid = _decompose(store, root, n=1)
    child = store.daemon.query_one("SELECT id FROM question WHERE parent_id=?", (_qnum(root),))[0]
    _force_answer(store, f"q{child}", cid)        # 关掉子问题
    store.resolve_deps()
    assert store.is_schedulable(root) is True      # dep satisfied → 父回可调度（聚合轮）
    # baseline dep
    with store.daemon.transaction() as conn:
        conn.execute("INSERT INTO baseline(id,slug,canonical_key,status) VALUES (1,'b','bk','planned')")
    store.record_question_dep(root, dep_type="baseline", target="1")
    assert store.is_schedulable(root) is False     # baseline 未 legal → pending
    with store.daemon.transaction() as conn:
        conn.execute("UPDATE baseline SET status='legal' WHERE id=1")
    store.resolve_deps()
    assert store.is_schedulable(root) is True


def test_record_dep_rejects_self_and_missing(store):
    root = _bootstrap_root(store)
    with pytest.raises(ValueError, match="禁自依赖"):
        store.record_question_dep(root, dep_type="question", target=root)
    with pytest.raises(ValueError, match="dep 目标不存在"):
        store.record_question_dep(root, dep_type="question", target="q999")


def test_inconclusive_visit_and_attack_limit(store):
    root = _bootstrap_root(store)
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    store.activate_question(root); store.mark_inconclusive(root)
    assert store.daemon.query_one("SELECT status,visit_count FROM question WHERE id=?", (_qnum(root),)) == ("inconclusive", 1)
    first = load_inconclusive_streak(
        store.daemon.conn, question_id=_qnum(root))
    assert first["consecutive_inconclusive"] == 1
    assert len(first["decision_ids"]) == 1
    store.mark_cycle_done(c.cycle_id)
    c2 = store.open_or_resume_cycle(); store.set_route(c2.cycle_id, "attack")
    store.activate_question(root); store.mark_inconclusive(root)   # visit=2 → 到 attack 限
    second = load_inconclusive_streak(
        store.daemon.conn, question_id=_qnum(root))
    assert second["consecutive_inconclusive"] == 2
    assert len(second["decision_ids"]) == 2
    assert store.is_schedulable(root, for_intent="attack") is False
    assert store.is_schedulable(root, for_intent="decompose") is True   # 到限仍可 decompose


def test_inconclusive_streak_resets_on_goal_version(store):
    root = _bootstrap_root(store)
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    store.activate_question(root); store.mark_inconclusive(root)
    store.mark_cycle_done(c.cycle_id)
    assert load_inconclusive_streak(
        store.daemon.conn, question_id=_qnum(root))[
            "consecutive_inconclusive"] == 1

    with store.daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO goal(id,version,text,predicate_json) "
            "VALUES (1,2,'amended','{}')")
        conn.execute("UPDATE question SET goal_ver=2 WHERE id=?", (_qnum(root),))
    progress = load_inconclusive_streak(
        store.daemon.conn, question_id=_qnum(root))
    assert progress["visit_count"] == 1
    assert progress["consecutive_inconclusive"] == 0
    assert progress["decision_ids"] == []


# ============ spawn / prune / goal_amend / applicability ============
def test_spawn_and_prune(store):
    root = _bootstrap_root(store)
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    store.apply_tree_ops(c.cycle_id, [{"op": "spawn_question", "kind": "followup", "parent_question_id": root,
                                       "text": "follow", "local_key": "f"}])
    spawned = store.daemon.query_one("SELECT id,source FROM question WHERE text='follow'")
    assert spawned[1] == "agent"
    store.record_question_dep(root, dep_type="question", target=f"q{spawned[0]}")
    assert store.is_schedulable(root, for_intent="attack") is False
    store.apply_tree_ops(c.cycle_id, [{"op": "propose_prune", "question_id": f"q{spawned[0]}", "reason_md": "no"}])
    assert store.daemon.query_one("SELECT status FROM question WHERE id=?", (spawned[0],))[0] == "dead_end"
    assert store.daemon.query_one("SELECT count(*) FROM decision WHERE type='prune_branch' AND question_id=?", (spawned[0],))[0] == 1
    assert store.daemon.query_one(
        "SELECT status FROM question_dep WHERE question_id=? AND depends_on_question_id=?",
        (_qnum(root), spawned[0])) == ("blocked",)
    assert store.is_schedulable(root, for_intent="attack") is True


def test_goal_amend_spawn_cap_counts_only_goal_amend_route(store):
    """goal_amend spawn 上限由本轮 durable decisions 计数，分批调用/重启不能绕过。"""
    root = _bootstrap_root(store)
    c = store.open_or_resume_cycle()
    store.set_route(c.cycle_id, "attack")
    store.apply_tree_ops(c.cycle_id, [{"op": "spawn_question", "kind": "followup",
                                       "parent_question_id": root, "text": "f", "local_key": "f"}])
    store.mark_cycle_done(c.cycle_id)
    c, amend, _ = _start_goal_amend(store)
    store.apply_tree_ops(c.cycle_id, [amend])
    for i in (1, 2):   # cap=2：两个 goal_retarget 应成功（attack 那次不占额）
        store.apply_tree_ops(c.cycle_id, [{"op": "spawn_question", "kind": "goal_retarget",
                                           "text": f"r{i}", "local_key": f"r{i}"}])
    with pytest.raises(ValueError, match="max_spawn_from_goal_amend"):   # 第 3 个超 cap
        store.apply_tree_ops(c.cycle_id, [{"op": "spawn_question", "kind": "goal_retarget",
                                           "text": "r3", "local_key": "r3"}])


def test_amend_goal_bumps_version(store):
    root = _bootstrap_root(store)
    with store.daemon.transaction() as conn:
        conn.execute("UPDATE question SET score=0.9,est_cost=2 WHERE id=?", (_qnum(root),))
    c, amend, did = _start_goal_amend(store, predicate={"k": 2})
    store.apply_tree_ops(c.cycle_id, [amend])
    assert store.daemon.query_one("SELECT max(version) FROM goal WHERE id=1")[0] == 2
    assert store.daemon.query_one("SELECT goal_ver,score,est_cost FROM question WHERE id=?", (_qnum(root),)) == (2, None, None)
    assert store.daemon.query_one(
        "SELECT previous_version,created_cycle,directive_id FROM goal WHERE id=1 AND version=2") == (1, _cnum(c.cycle_id), did)
    assert store.daemon.query_one("SELECT goal_ver FROM cycle WHERE id=?", (_cnum(c.cycle_id),))[0] == 2


def test_mark_answer_applicability_binding(store):
    root = _bootstrap_root(store)
    cid = _decompose(store, root, n=1)
    child = f"q{store.daemon.query_one('SELECT id FROM question WHERE parent_id=?', (_qnum(root),))[0]}"
    aid = _force_answer(store, child, cid)
    store.resolve_deps(); store.mark_cycle_done(cid)
    # 建 revalidate 回看题（parent=被回看 answer 所属问题=child）
    c, amend, _ = _start_goal_amend(store)
    store.apply_tree_ops(c.cycle_id, [amend,
        {"op": "spawn_question", "kind": "revalidate", "parent_question_id": child,
         "text": "reval", "local_key": "rv"}])
    store.apply_tree_ops(c.cycle_id, [{"op": "mark_answer_applicability", "answer_id": aid, "status": "needs_revalidation",
                                       "spawned_question_ref": "rv", "rationale_md": "why"}])
    row = store.daemon.query_one("SELECT status FROM answer_applicability WHERE answer_id=?", (_anum(aid),))
    assert row[0] == "needs_revalidation"


def test_mark_applicability_rejects_wrong_revalidate_parent(store):
    root = _bootstrap_root(store)
    cid = _decompose(store, root, n=1)
    child = f"q{store.daemon.query_one('SELECT id FROM question WHERE parent_id=?', (_qnum(root),))[0]}"
    aid = _force_answer(store, child, cid)
    with store.daemon.transaction() as conn:
        wrong = conn.execute(
            "INSERT INTO question(goal_id,goal_ver,born_goal_ver,text,status,source) "
            "VALUES (1,1,1,'other closed question','open','agent')").lastrowid
    wrong_qid = f"q{wrong}"
    _force_answer(store, wrong_qid, cid)
    store.resolve_deps(); store.mark_cycle_done(cid)
    c, amend, _ = _start_goal_amend(store)
    # 回看题挂在另一个合法 closed answer 下（≠ 被回看 answer 所属问题 child）→ 绑定拒绝
    store.apply_tree_ops(c.cycle_id, [amend,
        {"op": "spawn_question", "kind": "revalidate", "parent_question_id": wrong_qid,
         "text": "reval", "local_key": "rv"}])
    with pytest.raises(ValueError, match="回看题 parent"):
        store.apply_tree_ops(c.cycle_id, [{"op": "mark_answer_applicability", "answer_id": aid,
                                           "status": "contradicted", "spawned_question_ref": "rv", "rationale_md": "x"}])


# ============ selection ============
def test_persist_selection_terminate_and_attack(store):
    root = _bootstrap_root(store)
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    from orchestrator.interfaces import Selection
    store.persist_selection(c.cycle_id, Selection(next_question_id=root, next_intent="attack",
                                                  scores=[{"question_id": root, "score": 0.8, "est_cost": 1.0}]))
    assert store.daemon.query_one("SELECT next_question_id,next_intent FROM cycle WHERE id=?", (_cnum(c.cycle_id),)) == (_qnum(root), "attack")
    assert store.daemon.query_one("SELECT score,est_cost FROM question WHERE id=?", (_qnum(root),)) == (0.8, 1.0)
    store.persist_selection(c.cycle_id, Selection(next_question_id=None, next_intent="terminate", scores=[]))
    assert store.daemon.query_one("SELECT next_question_id,next_intent FROM cycle WHERE id=?", (_cnum(c.cycle_id),)) == (None, "terminate")


def test_persist_selection_local_key_resolution(store):
    root = _bootstrap_root(store)
    from orchestrator.interfaces import Selection
    cid = _decompose(store, root, n=1)                       # 建 child，local_key 'ch0' 入 _local_maps
    # 同轮 selection 引用 local_key 'ch0'（child 是 open、无 pending dep → 可调度）
    store.persist_selection(cid, Selection(next_question_id="ch0", next_intent="attack", scores=[]))
    child = store.daemon.query_one("SELECT id FROM question WHERE parent_id=?", (_qnum(root),))[0]
    assert store.daemon.query_one("SELECT next_question_id FROM cycle WHERE id=?", (_cnum(cid),))[0] == child


def test_persist_selection_terminate_requires_null(store):
    root = _bootstrap_root(store)
    from orchestrator.interfaces import Selection
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "attack")
    with pytest.raises(ValueError, match="terminate 时 next_question_id 必须为 null"):
        store.persist_selection(c.cycle_id, Selection(next_question_id=root, next_intent="terminate", scores=[]))


# ============ atomic() 跨方法 + close_question 未实现 ============
def test_atomic_spans_methods_rollback(store):
    """atomic() 内多方法共事务：后一步失败 → 前一步也回滚。"""
    root = _bootstrap_root(store)
    from orchestrator.interfaces import Selection
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "decompose"); store.activate_question(root)
    with pytest.raises(ValueError):
        with store.atomic():
            store.apply_tree_ops(c.cycle_id, [{"op": "add_children", "parent_question_id": root,
                                               "children": [{"local_key": "a", "text": "a"}]}])
            store.persist_selection(c.cycle_id, Selection(next_question_id="q999", next_intent="attack", scores=[]))  # 悬空 → 抛
    # add_children 已在同一事务、随 persist 失败一起回滚
    assert store.daemon.query_one("SELECT count(*) FROM question WHERE parent_id=?", (_qnum(root),))[0] == 0
    assert store.daemon.query_one("SELECT status FROM question WHERE id=?", (_qnum(root),))[0] == "active"


def test_close_question_not_implemented_points_to_gate(store):
    with pytest.raises(NotImplementedError, match="gate_close_question"):
        store.close_question("c1", "q1", "answered", [{"kind": "literature"}], "md")


def test_local_map_rolls_back_with_transaction_no_stale_alias(store):
    """回归（内审 BLOCKER）：apply_tree_ops 失败整批回滚时，进程内 _local_maps 也须复原——
    否则 SQLite 复用回滚 rowid，陈旧 local_key 会静默错绑到后来占用同 rowid 的别的问题。"""
    from orchestrator.interfaces import Selection
    root = _bootstrap_root(store)
    c = store.open_or_resume_cycle(); store.set_route(c.cycle_id, "decompose"); store.activate_question(root)
    cid = c.cycle_id
    # batchA：先写 child(local_key 'X')，再一个必失败 op → 整批回滚
    with pytest.raises(ValueError, match="不存在"):
        store.apply_tree_ops(cid, [
            {"op": "add_children", "parent_question_id": root, "children": [{"local_key": "X", "text": "cx"}]},
            {"op": "propose_prune", "question_id": "q999", "reason_md": "boom"}])
    assert "X" not in store._local_maps.get(cid, {}), "回滚后陈旧 local_key 'X' 不该残留"
    assert store.daemon.query_one("SELECT count(*) FROM question WHERE parent_id=?", (_qnum(root),))[0] == 0
    # batchB：成功写 child(local_key 'Y')——SQLite 复用刚释放的 rowid，'Y' 指向该新问题
    store.apply_tree_ops(cid, [{"op": "add_children", "parent_question_id": root,
                                "children": [{"local_key": "Y", "text": "cy"}]}])
    childB = store.daemon.query_one("SELECT id FROM question WHERE parent_id=?", (_qnum(root),))[0]
    assert store._local_maps[cid] == {"Y": f"q{childB}"}
    # 关键断言：selection 引用陈旧 'X' → 干净拒（而非静默绑到 childB）
    with pytest.raises(ValueError, match="缺失或不存在"):
        store.persist_selection(cid, Selection(next_question_id="X", next_intent="attack", scores=[]))


# ============ kill-9 无半写（M1b 验收核心） ============
def test_kill9_mid_decompose_no_half_write(tmp_path):
    """子进程在 decompose 事务（写子问题+释放父，未提交）中途被 kill -9 → 重开库无半写：
    子问题不存在、父仍 active（未释放）。证「add_children 同一事务」的崩溃安全（§4.2.5）。"""
    dbpath = tmp_path / "research.sqlite"
    # 父进程先备好：goal + decompose 轮 + active 父问题（均已提交）
    s = _new_store(str(dbpath))
    s.create_goal(text="g", predicate_json={})
    root = _bootstrap_root(s)
    c = s.open_or_resume_cycle(); s.set_route(c.cycle_id, "decompose"); s.activate_question(root)
    s.daemon.conn.close()

    marker = tmp_path / "ready.flag"
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(SYSTEM_ROOT)!r})
        from orchestrator import database as db
        from orchestrator.statestore_sqlite import SQLiteStateStore
        from orchestrator.writedaemon import WriteDaemon
        pol = {TEST_POLICY!r}
        s = SQLiteStateStore(WriteDaemon(db.connect({str(dbpath)!r})), pol)
        with s.atomic():                       # apply_tree_ops 写子问题+释放父，落在 atomic 外层事务
            s.apply_tree_ops({c.cycle_id!r}, [{{"op":"add_children","parent_question_id":{root!r},
                                                "children":[{{"local_key":"a","text":"child"}}]}}])
            open({str(marker)!r}, "w").close() # 信号：已写、未提交
            time.sleep(60)                      # 挂起等 kill -9（atomic 永不到达 COMMIT）
    """), encoding="utf-8")

    proc = subprocess.Popen([sys.executable, str(worker)])
    try:
        for _ in range(100):                    # 等子进程写完子问题、进入挂起
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists(), "worker 未到达挂起点"
        proc.kill()                             # SIGKILL：不给 ROLLBACK 机会
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    # 重开库核对：事务未提交 → SQLite 崩溃恢复丢弃它 → 无半写
    s2 = _new_store(str(dbpath))
    assert s2.daemon.query_one("SELECT count(*) FROM question WHERE parent_id=?", (_qnum(root),))[0] == 0, "子问题不该落库"
    assert s2.daemon.query_one("SELECT status FROM question WHERE id=?", (_qnum(root),))[0] == "active", "父不该被释放"
