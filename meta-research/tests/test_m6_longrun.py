"""CP7.5 · M6 长跑步级验证（§7.1 M6）——数百轮无人值守不漂移 + 可恢复 + τ 自停。

§7.1 M6「数百轮无人值守不漂移」的可证伪落点（reasoning-only 全自动，真组件 + mock provider）：
- **不漂移**：跑到**问题守卫上界**（max_open_questions/max_decompose_depth/max_children_per_node，
  §policy.tree_guard）后结构一致——**依赖引用完整、父指针有效、所有 cycle 终态 done、终轮 terminate**；
  无孤儿/悬挂依赖/半态残留。（goal_ver 恒=1 是 reasoning-only 结构必然，不作漂移断言，见下 §7.1 说明）
- **可恢复（durable 交接）**：整段跑 vs 中途换新实例续跑 → 终库（问题树 + cycle 序，排非确定字段）一致
  ——「无进程内记忆、状态在 DB」成立（**这三性质与轮数无关，是尺度不变的**）。
- **τ 自停**：provider 永不 terminate（价值衰退）时，编排器经安全网自终止，不空转到 max_cycles。

**轮数说明**：pure reasoning-only 循环受问题守卫上界约束（本 mock 广度填树至 ~max_open_questions
轮即收口）——生产「数百轮」来自 **attack 轮答问关题、前沿更新**（受 plan 契约缺口阻塞，ROADMAP 载）。
可恢复 / τ 自停两性质**尺度不变**（同一代码路径，与轮数无关，进程内计数无界增长已在 mark_cycle_done
清零，statestore「防长跑无界增长」）。**不漂移**在本尺度只覆盖 reasoning-only 可达的**结构 / 投影**
漂移模式（依赖引用完整、父指针有效、投影每轮归零）；attack 轮才引入的表增长类漂移需真长跑（阻于
plan 契约），不在本套。provider 从 **DB 状态**确定性派生（非进程内计数）——故中途换实例续跑产同一树。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from orchestrator import database as db
from orchestrator.advancer import SqliteAdvancer
from orchestrator.compiler_sqlite import SqliteCompiler
from orchestrator.statestore_sqlite import SQLiteStateStore
from orchestrator.stopcontroller import StopController
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
# Production currently permits unbounded depth.  This focused guard-envelope
# test deliberately installs a finite depth so it can still prove the bounded
# variant and terminate deterministically.
if POLICY["tree_guard"]["max_decompose_depth"] is None:
    POLICY["tree_guard"]["max_decompose_depth"] = 4

MAX_DEPTH = POLICY["tree_guard"]["max_decompose_depth"]
MAX_OPEN = POLICY["tree_guard"]["max_open_questions"]

# 最低 id 的**可 decompose 叶**（open + 无子 = 无 pending 子依赖 = 可调度；depth < MAX_DEPTH 才可再分解，
# 否则子会超 depth 上界）。decompose 后父被自身的子依赖锁住（不可再选），只有叶可调度、且分解会加深——
# 故守卫下 pure reasoning-only 是有限深度树（广度由「取最低 id 叶」= BFS 序自然铺开）。
_LEAF_SQL = f"""
WITH RECURSIVE d(id, depth) AS (
  SELECT id, 0 FROM question WHERE parent_id IS NULL
  UNION ALL SELECT q.id, d.depth+1 FROM question q JOIN d ON q.parent_id=d.id)
SELECT q.id FROM question q JOIN d ON d.id=q.id
WHERE q.status='open' AND d.depth < {MAX_DEPTH}
  AND NOT EXISTS (SELECT 1 FROM question c WHERE c.parent_id=q.id) ORDER BY q.id LIMIT 1"""


def _bounded_provider(daemon, *, terminate=True, score=0.9):
    """DB 确定性**广度**decompose provider（守卫安全）：bootstrap 造根+选 decompose；每 decompose 轮给活跃
    叶挂 2 子，再选**最低 id 的可分解叶**（open+无子+depth<MAX_DEPTH，BFS 序）作下一目标；无可分解叶或
    问题数近 max_open 上界 → terminate（terminate=False 则永不停，供 τ 自停）。全从 DB 派生 → 换实例续跑
    产同一树。**轮数受守卫上界约束**（有限深度树，见模块 docstring 轮数说明）。"""
    def provider(cyc, pack):
        if cyc.route == "bootstrap":
            return {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题", "local_key": "r"}]},
                    "selection.json": {"next_question_id": "r", "next_intent": "decompose",
                                       "scores": [{"question_id": "r", "score": score, "est_cost": 1.0}]}}
        nq = daemon.query_one("SELECT count(*) FROM question")[0]
        cyc_depth = daemon.query_one(
            "WITH RECURSIVE d(id,depth) AS (SELECT id,0 FROM question WHERE parent_id IS NULL "
            "UNION ALL SELECT q.id,d.depth+1 FROM question q JOIN d ON q.parent_id=d.id) "
            "SELECT depth FROM d WHERE id=?", (int(cyc.question_id[1:]),))[0]
        lk1, lk2 = f"q{nq}a", f"q{nq}b"                       # 本轮新挂 2 子的 local_key（按问题数命名，确定性）
        ops = [{"op": "add_children", "parent_question_id": cyc.question_id,
                "children": [{"text": f"子 {lk1}", "local_key": lk1}, {"text": f"子 {lk2}", "local_key": lk2}]}]
        # 两个新子都评分（否则未评分的兄弟会让 τ 判据①（前沿须全评分）永重置——CP7.1 保守面）
        child_scores = [{"question_id": lk1, "score": score, "est_cost": 1.0},
                        {"question_id": lk2, "score": score, "est_cost": 1.0}]
        nxt = daemon.query_one(_LEAF_SQL)                     # 既有可分解浅叶（当前 active 的 cyc 已自动排除）
        children_decomposable = cyc_depth + 1 < MAX_DEPTH     # 本轮新子是否还可再分解（depth 上界内）
        near_cap = nq + 2 >= MAX_OPEN - 2                     # 问题数近 max_open 上界
        if terminate and (near_cap or (nxt is None and not children_decomposable)):
            sel = {"next_question_id": None, "next_intent": "terminate", "scores": child_scores,
                   "terminate_reason_md": "守卫上界内探索完毕，收口"}
        else:
            tgt = f"q{nxt[0]}" if nxt is not None else lk1    # 有既有浅叶选之；否则选本轮新子（含 τ 永不停用例）
            sel = {"next_question_id": tgt, "next_intent": "decompose", "scores": child_scores}
        return {"tree_ops.json": {"ops": ops}, "selection.json": sel}
    return provider


def _mk(path):
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="长跑目标", predicate_json={})
    compiler = SqliteCompiler(db.connect(path), POLICY)
    return daemon, state, compiler


def _tree_state(path):
    """终库确定性面（排非确定字段：id 序保留但内容按 text 对，排 timestamp）。cycle 纳入 active_question_id
    /next_question_id（外审 SHOULD：验中途恢复后调度的**是同一问题序**，非仅最终树形碰巧一致）+ question_dep
    确定性投影。"""
    c = db.connect(path)
    questions = c.execute("SELECT text, parent_id, status, goal_ver FROM question ORDER BY id").fetchall()
    cycles = c.execute("SELECT status, route, active_question_id, next_question_id, next_intent "
                       "FROM cycle ORDER BY id").fetchall()
    deps = c.execute("SELECT question_id, depends_on_question_id, status FROM question_dep ORDER BY id").fetchall()
    c.close()
    return {"questions": questions, "cycles": cycles, "deps": deps}


# ============ 长跑（守卫上界内）无漂移 ============
def test_longrun_guard_envelope_no_drift(tmp_path):
    """跑满问题守卫上界（~max_open 问题、十余轮）后结构无漂移。轮数受守卫约束，见模块 docstring
    「轮数说明」（生产数百轮来自 attack 答问+前沿更新）——本套验尺度不变的不漂移性质。"""
    path = str(tmp_path / "lr.sqlite")
    daemon, state, compiler = _mk(path)
    adv = SqliteAdvancer(state, compiler, _bounded_provider(daemon))
    ids = adv.run_cycles(max_cycles=300)
    assert len(ids) >= 12                                  # 真跑满守卫上界（十余轮广度填树）

    # 不漂移①：question_dep 引用完整——每条依赖指向存在的问题（十余轮累积无悬挂/错乱依赖）。
    # （注：goal_ver 恒=1 是 reasoning-only 结构必然[无 goal_amend 路由]，不作漂移断言——见 §7.1 M6 说明）
    assert daemon.query_one(
        "SELECT count(*) FROM question_dep WHERE dep_type='question' AND depends_on_question_id NOT IN "
        "(SELECT id FROM question)")[0] == 0
    # 不漂移②：每个非根问题的 parent_id 指向存在的问题（树有效、无孤儿）
    assert daemon.query_one(
        "SELECT count(*) FROM question q WHERE q.parent_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM question p WHERE p.id=q.parent_id)")[0] == 0
    # 不漂移③：所有 cycle 终态 done（无半态残留），终轮 terminate
    assert daemon.query_one("SELECT count(*) FROM cycle WHERE status<>'done'")[0] == 0
    assert state.last_done_cycle().next_intent == "terminate"
    # 恰有一个根（parent 空）；问题数填至守卫上界量级
    assert daemon.query_one("SELECT count(*) FROM question WHERE parent_id IS NULL")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM question")[0] >= 20


# ============ 可恢复：整段 vs 分段续跑，终库一致 ============
def test_longrun_restart_equivalence(tmp_path):
    ref, seg = str(tmp_path / "ref.sqlite"), str(tmp_path / "seg.sqlite")
    # 参考：一口气跑到 done（本 provider 约 15 轮填满守卫树再 terminate）
    d0, s0, c0 = _mk(ref)
    full = SqliteAdvancer(s0, c0, _bounded_provider(d0)).run_cycles(max_cycles=200)
    # 分段：**只跑一半**（max_cycles=6 < 全程 ~15，故在途未 terminate），再换**新实例**（模拟重启、
    # 无进程内记忆）续跑到 done——这才真验中途重启的 durable 交接（非跑完再空 resume）
    d1, s1, c1 = _mk(seg)
    seg1 = SqliteAdvancer(s1, c1, _bounded_provider(d1)).run_cycles(max_cycles=6)
    assert 0 < len(seg1) < len(full) and s1.last_done_cycle().next_intent != "terminate"  # 确在途、未收口
    d2 = WriteDaemon(db.connect(seg)); s2 = SQLiteStateStore(d2, POLICY)
    c2 = SqliteCompiler(db.connect(seg), POLICY)
    seg2 = SqliteAdvancer(s2, c2, _bounded_provider(d2)).run_cycles(max_cycles=200)   # 新实例续跑到 done
    assert len(seg2) > 0                                      # 续跑段真推进（防退化为「跑完再空 resume」的空真）

    assert _tree_state(ref) == _tree_state(seg)               # 终库问题树 + cycle 序逐字节一致（durable 交接）


# ============ τ 自停（价值衰退，provider 永不 terminate）============
def test_longrun_tau_selfstop_on_decay(tmp_path):
    path = str(tmp_path / "tau.sqlite")
    daemon, state, compiler = _mk(path)
    stop = StopController(daemon, POLICY)
    stop.score_floor = 0.25
    stop.consecutive_rounds = 3
    # 永不 terminate + 低分（0.1<floor）→ 连续衰退 → τ 判据① 自停
    adv = SqliteAdvancer(state, compiler, _bounded_provider(daemon, terminate=False, score=0.1),
                         stop_controller=stop)
    ids = adv.run_cycles(max_cycles=500)
    assert adv.last_stop_reason == "score_floor"              # 安全网自停（非跑满 500）
    assert len(ids) < 500 and stop.already_stopped() == "score_floor"
    ncyc = daemon.query_one("SELECT count(*) FROM cycle")[0]
    # 重启仍拒推进（durable 停机）——**全新实例**（重开 DB + 新 daemon/state/compiler/StopController），
    # 证停机事实在 DB 而非对象内存缓存（外审 BLOCKER）
    d2 = WriteDaemon(db.connect(path)); s2 = SQLiteStateStore(d2, POLICY)
    c2 = SqliteCompiler(db.connect(path), POLICY)
    adv2 = SqliteAdvancer(s2, c2, _bounded_provider(d2, terminate=False, score=0.1),
                          stop_controller=StopController(d2, POLICY))
    assert adv2.run_cycles(max_cycles=10) == []              # 新实例读 DB 里的 global_stop → 拒推进
    assert d2.query_one("SELECT count(*) FROM cycle")[0] == ncyc   # cycle 数未增（真未推进）
