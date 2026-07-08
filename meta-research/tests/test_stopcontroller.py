"""CP7.1 · StopController 长跑自终止安全网（§4.4.6 τ 三判据；M6）。

核心验收面（§7.1 M6「数百轮无人值守不漂移」的自终止基础）：判据②预算耗尽（DB 派生、恢复安全）；
判据①分数连续 N 轮 < floor（进程内计数，触发落 durable global_stop）；停机 durable（重启拒推进）；
run_cycles 集成——自停后返回、不再开新轮。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator.stopcontroller import StopController
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _policy(**over):
    import copy
    p = copy.deepcopy(POLICY)
    p.setdefault("flow", {}).setdefault("tau", {})
    for k, v in over.items():
        if k in ("score_floor", "consecutive_rounds"):
            p["flow"]["tau"][k] = v
        elif k == "session_max":
            p["budget"]["session_max"] = v
    return p


@pytest.fixture()
def daemon():
    d = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(d.conn)
    return d


# ============ 判据②：预算耗尽（durable、DB 派生）============
def _add_ledger(daemon, money, phase="reasoning"):
    """向全局成本台账 ledger 记一笔（判据②权威源；覆盖 reasoning/Codex 等全 phase）。"""
    with daemon.transaction() as conn:
        conn.execute("INSERT INTO ledger(cycle_id,phase,run_id,money,policy_version) "
                     "VALUES (1,?,1,?,'v0')", (phase, money))


def test_budget_exhausted_stops(daemon):
    sc = StopController(daemon, _policy(session_max=10))
    assert sc.check_after_round() is None                      # ledger 空 → SUM=0（休眠）
    _add_ledger(daemon, 6); _add_ledger(daemon, 5)             # 合计 11 ≥ 10（含 reasoning 成本）
    hit = sc.check_after_round()
    assert hit["reason"] == "budget_exhausted" and hit["spent"] == 11
    assert sc.already_stopped() == "budget_exhausted"          # 落 durable global_stop


def test_budget_disabled_when_null(daemon):
    sc = StopController(daemon, _policy(session_max=None))
    _add_ledger(daemon, 999999)
    assert sc.check_after_round() is None                      # null 关闭预算安全网


# ============ 判据①：分数连续衰退 ============
def test_score_floor_streak_stops(daemon):
    sc = StopController(daemon, _policy(score_floor=0.25, consecutive_rounds=3, session_max=None))
    with daemon.transaction() as conn:   # 一个 open 低分题（seed 的 q1 是 active，另建 open）
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source,score) "
                     "VALUES (9,1,1,1,'低分 open','open','agent',0.1)")
    assert sc.check_after_round() is None                      # 第 1 轮 < floor
    assert sc.check_after_round() is None                      # 第 2 轮
    hit = sc.check_after_round()                               # 第 3 轮达阈
    assert hit["reason"] == "score_floor" and hit["streak"] == 3
    assert sc.already_stopped() == "score_floor"


def test_score_floor_streak_resets_on_recovery_value(daemon):
    """高分打断连续计数（衰退非单调）：不误停。"""
    sc = StopController(daemon, _policy(score_floor=0.25, consecutive_rounds=3, session_max=None))
    with daemon.transaction() as conn:
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source,score) "
                     "VALUES (9,1,1,1,'q','open','agent',0.1)")
    sc.check_after_round(); sc.check_after_round()             # streak=2
    with daemon.transaction() as conn:
        conn.execute("UPDATE question SET score=0.9 WHERE id=9")   # 回升
    assert sc.check_after_round() is None                      # 打断 → 重置
    with daemon.transaction() as conn:
        conn.execute("UPDATE question SET score=0.1 WHERE id=9")
    assert sc.check_after_round() is None                      # 重新计数：streak=1
    assert sc.check_after_round() is None                      # 2
    assert sc.check_after_round()["streak"] == 3               # 3 → 停


def test_null_scores_not_decay(daemon):
    """全 NULL 分的 open 题（新题未评）不算衰退——防早轮误停。"""
    sc = StopController(daemon, _policy(score_floor=0.25, consecutive_rounds=2, session_max=None))
    with daemon.transaction() as conn:
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                     "VALUES (9,1,1,1,'未评 open','open','agent')")   # score NULL
    assert sc.check_after_round() is None
    assert sc.check_after_round() is None                      # 不因 NULL 触发


def test_null_mixed_with_low_score_not_decay(daemon):
    """内审 BLOCKER 回归：前沿=一个低分 + 一个**未评分**（NULL）→ 不算衰退（未评估价值未知≠低于 τ）。
    旧实现只取非空分 MAX=0.1<floor 会误停一条活跃研究——durable 不可恢复。"""
    sc = StopController(daemon, _policy(score_floor=0.25, consecutive_rounds=1, session_max=None))
    with daemon.transaction() as conn:
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source,score) "
                     "VALUES (9,1,1,1,'低分','open','agent',0.1)")
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                     "VALUES (10,1,1,1,'未评','open','agent')")        # NULL 分
    assert sc.check_after_round() is None                      # 混合前沿不误停
    with daemon.transaction() as conn:                         # 补评后仍低 → 才算衰退
        conn.execute("UPDATE question SET score=0.1 WHERE id=10")
    assert sc.check_after_round()["reason"] == "score_floor"   # 前沿全评分且皆低 → 停


def test_score_floor_includes_active_scope(daemon):
    """内审 SHOULD 回归：作用域含 active（§4.4.6/ROADMAP「open/active 最高分」）——高分 active 不误停。"""
    sc = StopController(daemon, _policy(score_floor=0.25, consecutive_rounds=1, session_max=None))
    with daemon.transaction() as conn:   # seed q1 已 answered（无前沿）；造高分 active + 低分 open
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source,score) "
                     "VALUES (9,1,1,1,'高分 active','active','agent',0.9)")
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source,score) "
                     "VALUES (10,1,1,1,'低分 open','open','agent',0.1)")
    assert sc.check_after_round() is None                      # 含 active：MAX=0.9 ≥ floor，不停


def test_no_open_questions_not_decay(daemon):
    """无 open/active 题（seed q1 已 answered）不算分数衰退——交由正常 terminate 路径，不误报 score_floor。"""
    sc = StopController(daemon, _policy(score_floor=0.25, consecutive_rounds=1, session_max=None))
    assert sc.check_after_round() is None                      # 无 open/active 前沿 → 非衰退


# ============ durable 停机 ============
def test_record_stop_idempotent(daemon):
    sc = StopController(daemon, _policy(session_max=1))
    sc.record_stop("budget_exhausted")
    sc.record_stop("budget_exhausted")
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='global_stop'")[0] == 1


# ============ run_cycles 集成 ============
def _mk_advancer(daemon, stop_controller):
    from orchestrator.advancer import SqliteAdvancer
    from orchestrator.compiler_sqlite import SqliteCompiler
    from orchestrator.statestore_sqlite import SQLiteStateStore
    # 独立文件库不便，这里复用 daemon 连接构造真组件；provider 造 create_root+terminate（最短轮）
    state = SQLiteStateStore(daemon, POLICY)

    def provider(cyc, pack):
        return {"tree_ops.json": {"ops": [{"op": "create_root", "text": "q", "local_key": "r"}]},
                "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    compiler = SqliteCompiler.__new__(SqliteCompiler)   # 不触发 render（provider 直接给产物）
    adv = SqliteAdvancer(state, compiler, provider, stop_controller=stop_controller)
    return adv


def test_run_cycles_refuses_when_already_stopped(daemon):
    sc = StopController(daemon, _policy(session_max=1))
    sc.record_stop("budget_exhausted")                        # 预先落停机
    adv = _mk_advancer(daemon, sc)
    assert adv.run_cycles(5) == []                            # 启动即拒推进
    assert adv.last_stop_reason == "budget_exhausted"


def test_run_cycles_stops_mid_run_on_score_floor(tmp_path):
    """安全网**跑中生效**：bootstrap 造低分根+选 decompose（本会继续第 2 轮），但分数衰退（floor=0.25,
    N=1）令 StopController 在第 1 轮完成后自停——decompose 轮不再开，last_stop_reason=score_floor。"""
    from orchestrator.advancer import SqliteAdvancer
    from orchestrator.compiler_sqlite import SqliteCompiler
    from orchestrator.statestore_sqlite import SQLiteStateStore
    path = str(tmp_path / "s.sqlite")
    d = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(d, POLICY)
    state.create_goal(text="g", predicate_json={})
    compiler = SqliteCompiler(db.connect(path), POLICY, goal_body_md="g")

    def low_provider(cyc, pack):   # 造根、低分、选 decompose（非 terminate → 本应续跑）
        return {"tree_ops.json": {"ops": [{"op": "create_root", "text": "低价值根", "local_key": "root"}]},
                "selection.json": {"next_question_id": "root", "next_intent": "decompose",
                                   "scores": [{"question_id": "root", "score": 0.1, "est_cost": 1.0}]}}

    sc = StopController(d, _policy(score_floor=0.25, consecutive_rounds=1, session_max=None))
    adv = SqliteAdvancer(state, compiler, low_provider, stop_controller=sc)
    ids = adv.run_cycles(max_cycles=8)
    assert len(ids) == 1                                      # 只跑了 bootstrap 轮；decompose 轮被安全网拦下
    assert adv.last_stop_reason == "score_floor"
    assert sc.already_stopped() == "score_floor"              # durable
    assert SqliteAdvancer(state, compiler, low_provider, stop_controller=sc).run_cycles(3) == []  # 重启仍拒


def test_run_cycles_budget_gate_stops_before_any_round(tmp_path):
    """外审 BLOCKER 回归：ledger 已超 session_max 但无 global_stop（模拟崩在落停机前）→ 重启 run_cycles
    **开工前**预算门立即停、不白跑一轮。"""
    from orchestrator.advancer import SqliteAdvancer
    from orchestrator.compiler_sqlite import SqliteCompiler
    from orchestrator.statestore_sqlite import SQLiteStateStore
    path = str(tmp_path / "b.sqlite")
    d = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(d, POLICY)
    state.create_goal(text="g", predicate_json={})
    compiler = SqliteCompiler(db.connect(path), POLICY, goal_body_md="g")
    calls = []

    def provider(cyc, pack):
        calls.append(cyc.cycle_id)   # 不该被调用：预算门在开工前拦下
        return {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根", "local_key": "root"}]},
                "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}

    with d.transaction() as conn:    # 直接把成本记入 ledger（无 global_stop）——模拟落停机前崩溃
        conn.execute("INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) VALUES (99,1,1,'done','v0')")
        rc = conn.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status) "
                          "VALUES (99,'reasoning','r','success')").lastrowid
        conn.execute("INSERT INTO ledger(cycle_id,phase,money,policy_version,runner_call_id) "
                     "VALUES (99,'reasoning',50,'v0',?)", (rc,))
    sc = StopController(d, _policy(session_max=10, score_floor=0.25, consecutive_rounds=3))
    adv = SqliteAdvancer(state, compiler, provider, stop_controller=sc)
    assert adv.run_cycles(5) == []
    assert calls == []                                        # provider 一次未调 = 未白跑
    assert adv.last_stop_reason == "budget_exhausted"
    assert sc.already_stopped() == "budget_exhausted"         # 补落 durable global_stop


def test_controller_does_not_break_provider_terminate(tmp_path):
    """NIT 回归：装了不触发的 StopController，provider terminate（判据③）照常停机——安全网不干扰正常收口。"""
    from orchestrator.advancer import SqliteAdvancer
    from orchestrator.compiler_sqlite import SqliteCompiler
    from orchestrator.statestore_sqlite import SQLiteStateStore
    path = str(tmp_path / "t.sqlite")
    d = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(d, POLICY)
    state.create_goal(text="g", predicate_json={})
    compiler = SqliteCompiler(db.connect(path), POLICY, goal_body_md="g")

    def high_provider(cyc, pack):   # 造高分根 + terminate（正常收口）
        return {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根", "local_key": "root"}]},
                "selection.json": {"next_question_id": None, "next_intent": "terminate",
                                   "scores": [{"question_id": "root", "score": 0.9, "est_cost": 1.0}]}}

    sc = StopController(d, _policy(score_floor=0.25, consecutive_rounds=1, session_max=100000))  # 高分不触发①、ledger 空不触发②
    adv = SqliteAdvancer(state, compiler, high_provider, stop_controller=sc)
    ids = adv.run_cycles(max_cycles=5)
    assert len(ids) == 1 and adv.last_stop_reason is None     # provider terminate 停机、非安全网
    assert sc.already_stopped() is None                       # 未落 global_stop
