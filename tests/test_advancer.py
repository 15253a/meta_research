"""CP4.1 · SqliteAdvancer（M3：derive_next_route 全矩阵 + advance bootstrap 创世轮 + 阶段原子/幂等）。

核心：
- derive_next_route 忠实 §6.13(3) 矩阵（所有 intent×outcome 行）。
- advance 驱动 bootstrap 轮：单一 atomic 事务落 create_root + selection + mark_done（真 SQLite）。
- **恢复语义前身**：阶段内失败 → 整事务回滚（create_root 亦回滚），cycle 停在阶段前状态；已终态 → 幂等 done。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orchestrator import database as db
from orchestrator.advancer import SqliteAdvancer, derive_next_route
from orchestrator.compiler_sqlite import SqliteCompiler
from orchestrator.interfaces import PlanOutcome, Selection
from orchestrator.statestore_sqlite import SQLiteStateStore
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


# ============ derive_next_route 全矩阵（§6.13(3)）============
def _sel(intent):
    return Selection(next_question_id=(None if intent == "terminate" else "q1"), next_intent=intent)


def test_route_terminate_none():
    assert derive_next_route(_sel("terminate"), PlanOutcome()) is None   # 停机、本轮 route 不改写


def test_route_decompose():
    assert derive_next_route(_sel("decompose"), PlanOutcome()) == "decompose"


def test_route_attack_build_exec():
    assert derive_next_route(_sel("attack"), PlanOutcome(has_build_or_exec=True)) == "attack"


def test_route_attack_only_eval():
    assert derive_next_route(_sel("attack"), PlanOutcome(only_eval=True)) == "eval_only"


def test_route_attack_empty_reuse():
    assert derive_next_route(_sel("attack"), PlanOutcome(empty_targets=True)) == "reuse_only"


def test_route_attack_blocked():
    assert derive_next_route(_sel("attack"), PlanOutcome(blocked=True)) == "dependency_wait"


def test_route_attack_import_deferred():
    assert derive_next_route(_sel("attack"), PlanOutcome(import_deferred=True)) == "dependency_wait"


def test_route_blocked_precedes_build():
    """blocked/import_deferred 优先于 build/exec → dependency_wait（本轮有未满足 dep 即先等）。"""
    assert derive_next_route(_sel("attack"),
                             PlanOutcome(has_build_or_exec=True, import_deferred=True)) == "dependency_wait"


def test_route_bad_intent_raises():
    with pytest.raises(ValueError, match="next_intent"):
        derive_next_route(Selection(next_question_id="q1", next_intent="bogus"), PlanOutcome())


def test_route_attack_unclassifiable_raises():
    """codex SHOULD：attack 且无任何 flag（含 empty_targets=False）→ fail closed（疑分类器 bug），不静默当全复用。"""
    with pytest.raises(ValueError, match="无法分类"):
        derive_next_route(_sel("attack"), PlanOutcome())   # 全 False：empty_targets 也 False


def test_durable_terminate_precedes_starting_new_import_work(env):
    state, compiler = env
    cyc = _prepared_bootstrap(state)

    def terminate_provider(_cyc, _pack):
        return {
            "tree_ops.json": {"ops": [
                {"op": "create_root", "text": "root", "local_key": "root"}]},
            "selection.json": {
                "next_question_id": None, "next_intent": "terminate", "scores": []},
        }

    SqliteAdvancer(state, compiler, terminate_provider).advance(cyc.cycle_id)

    class QueueMustNotRun:
        def materialize_pending(self, *, max_items=None):
            pytest.fail("durable terminate 后不得启动新的 import worker")

    adv = SqliteAdvancer(
        state, compiler, terminate_provider, import_worker=QueueMustNotRun())
    assert adv._resume_or_open() is None


# ============ advance bootstrap 创世轮（真 SQLite）============
@pytest.fixture()
def env(tmp_path):
    """真组件：WriteDaemon + SQLiteStateStore（写）+ SqliteCompiler（独立读连接，须文件库共享）。"""
    path = str(tmp_path / "research.sqlite")
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="EEG 跨数据集通用规律研究", predicate_json={})
    compiler = SqliteCompiler(db.connect(path), POLICY)
    return state, compiler


def _bootstrap_provider(cyc, pack):
    """确定性 reasoning 产物（bootstrap）：create_root + 选中 root attack。"""
    return {
        "tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题：EEG 有跨数据集通用规律吗？", "local_key": "root"}]},
        "selection.json": {"next_question_id": "root", "next_intent": "attack",
                           "scores": [{"question_id": "root", "score": 0.9, "est_cost": 1.0}]},
    }


def _prepared_bootstrap(state, route="bootstrap"):
    cyc = state.open_or_resume_cycle()
    state.set_route(cyc.cycle_id, route)
    return cyc


def test_advance_bootstrap_to_done(env):
    state, compiler = env
    cyc = _prepared_bootstrap(state)
    adv = SqliteAdvancer(state, compiler, _bootstrap_provider)
    assert adv.advance(cyc.cycle_id) == "done"
    reloaded = state.cycle(cyc.cycle_id)
    assert reloaded.status == "done"
    assert reloaded.next_intent == "attack" and reloaded.next_question_id is not None   # selection 落库
    q = state.list_schedulable_questions()   # root 仍 open（selection 不改其状态）
    assert len(q) == 1 and "通用规律" in q[0]["text"]
    assert state.daemon.query_one(
        "SELECT count(*) FROM phase_commit "
        "WHERE cycle_id=? AND stage='reasoning' AND target_id IS NULL",
        (int(cyc.cycle_id[1:]),))[0] == 1


def test_advance_idempotent_on_done(env):
    """已终态 cycle 再 advance → 幂等 done，**短路在 provider 之前**（恢复：已提交轮跳过、不重复写）。"""
    state, compiler = env
    cyc = _prepared_bootstrap(state)
    calls = {"n": 0}

    def counting_provider(c, pack):
        calls["n"] += 1
        return _bootstrap_provider(c, pack)

    adv = SqliteAdvancer(state, compiler, counting_provider)
    adv.advance(cyc.cycle_id)
    assert calls["n"] == 1
    n_q = len(state.list_schedulable_questions())
    assert adv.advance(cyc.cycle_id) == "done"          # 再调
    assert calls["n"] == 1                               # provider 未被再调（短路在 _reasoning_only 之前）
    assert len(state.list_schedulable_questions()) == n_q   # 无新增（未重复 create_root）


def test_advance_terminal_failed_short_circuits(env):
    """failed 轮 advance 亦返回 done、不触 provider（恢复游标：非 done 但已终态同样跳过）。"""
    state, compiler = env
    cyc = _prepared_bootstrap(state)
    state.mark_cycle_done(cyc.cycle_id, "failed")
    tripwire = lambda c, pack: (_ for _ in ()).throw(AssertionError("provider 不应被调用"))
    assert SqliteAdvancer(state, compiler, tripwire).advance(cyc.cycle_id) == "done"


def test_advance_atomic_rollback(env):
    """阶段内失败（selection 引用不存在问题）→ 整事务回滚：create_root 亦回滚、cycle 未 done（恢复语义前身）。"""
    state, compiler = env
    cyc = _prepared_bootstrap(state)

    def bad_provider(c, pack):
        return {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根", "local_key": "root"}]},
                "selection.json": {"next_question_id": "ghost", "next_intent": "attack", "scores": []}}

    adv = SqliteAdvancer(state, compiler, bad_provider)
    with pytest.raises(ValueError):
        adv.advance(cyc.cycle_id)
    assert state.cycle(cyc.cycle_id).status == "created"       # 未推进（回滚）
    assert state.list_schedulable_questions() == []            # create_root 随事务回滚（无残留根问题）


def test_advance_local_key_rebind_after_rollback(env):
    """codex SHOULD：坏 selection 致 create_root 'root' 回滚后，重试用同 local_key 'root' 的好 provider 须成功收尾。
    覆盖 **advancer 层 rollback/retry 路径**（含事务外 render→事务内校验+写的完整闭环）；
    _local_maps 投影错绑（rowid 复用）的**surgical 证明**在 statestore 专测 test_statestore_sqlite.py（本类正确依赖之）。"""
    state, compiler = env
    cyc = _prepared_bootstrap(state)
    # 第一次：create_root 'root' 后坏 selection（ghost）→ 整事务回滚，投影须复原（'root' 从 _local_maps 移除）
    bad = lambda c, p: {"tree_ops.json": {"ops": [{"op": "create_root", "text": "坏根（应回滚）", "local_key": "root"}]},
                        "selection.json": {"next_question_id": "ghost", "next_intent": "attack", "scores": []}}
    with pytest.raises(ValueError):
        SqliteAdvancer(state, compiler, bad).advance(cyc.cycle_id)
    assert state.list_schedulable_questions() == []          # 坏根已回滚
    # 重试：好 provider，同 local_key 'root' → 须解析到**本次**新建根、成功收尾（无错绑）
    SqliteAdvancer(state, compiler, _bootstrap_provider).advance(cyc.cycle_id)
    reloaded = state.cycle(cyc.cycle_id)
    qs = state.list_schedulable_questions()
    assert reloaded.status == "done"
    assert len(qs) == 1 and "通用规律" in qs[0]["text"]        # 好根，非坏根残留
    assert reloaded.next_question_id == qs[0]["question_id"]   # selection 'root' 正确重绑到本次新建根


def test_advance_bootstrap_requires_create_root(env):
    """codex SHOULD：bootstrap 无 create_root（如仅 terminate selection）→ fail closed，绝不标 done 却无根。"""
    state, compiler = env
    cyc = _prepared_bootstrap(state)
    no_root = lambda c, p: {"tree_ops.json": {"ops": []},
                            "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    with pytest.raises(ValueError, match="create_root"):
        SqliteAdvancer(state, compiler, no_root).advance(cyc.cycle_id)
    assert state.cycle(cyc.cycle_id).status == "created"      # 未标 done


def test_decompose_at_hard_depth_releases_and_reselects_atomically(env):
    state, compiler = env
    state.policy = yaml.safe_load(yaml.safe_dump(state.policy))
    state.policy["tree_guard"]["max_decompose_depth"] = 4
    with state.daemon.transaction() as conn:
        parent = None
        for index in range(5):
            parent = conn.execute(
                "INSERT INTO question(parent_id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                "VALUES (?,1,1,1,?,'open','agent')",
                (parent, f"depth-{index}"),).lastrowid
        alternate = conn.execute(
            "INSERT INTO question(goal_id,goal_ver,born_goal_ver,text,status,source) "
            "VALUES (1,1,1,'alternate','open','agent')").lastrowid

    cyc = state.open_or_resume_cycle()
    state.set_route(cyc.cycle_id, "decompose")
    state.activate_question(f"q{parent}")

    def fallback_provider(_cyc, _pack):
        return {
            "tree_ops.json": {"ops": []},
            "selection.json": {
                "next_question_id": f"q{alternate}",
                "next_intent": "attack",
                "scores": [],
            },
        }

    assert SqliteAdvancer(state, compiler, fallback_provider).advance(cyc.cycle_id) == "done"
    assert state.cycle(cyc.cycle_id).next_question_id == f"q{alternate}"
    assert state.daemon.query_one(
        "SELECT status FROM question WHERE id=?", (parent,))[0] == "open"
    assert state.daemon.query_one(
        "SELECT count(*) FROM decision WHERE cycle_id=? "
        "AND type='decompose_guard_fallback'", (int(cyc.cycle_id[1:]),))[0] == 1


def test_advance_attack_not_implemented(env):
    """CP4.2 仅 reasoning-only（bootstrap/decompose）；attack route → NotImplementedError（诚实拒，M4 接池注册/真执行）。"""
    state, compiler = env
    cyc = _prepared_bootstrap(state, route="attack")
    adv = SqliteAdvancer(state, compiler, _bootstrap_provider)
    with pytest.raises(NotImplementedError, match="attack"):
        adv.advance(cyc.cycle_id)


def _seed_dependency_wait(state, statuses):
    with state.daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version) "
            "VALUES (1,1,1,'done','dependency_wait','v0')")
        conn.execute(
            "INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
            "VALUES (1,1,1,1,'等待题','open','agent')")
        for dep_id, status in enumerate(statuses, start=2):
            conn.execute(
                "INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                "VALUES (?,1,1,1,?,'open','agent')", (dep_id, f"依赖{dep_id}"))
            conn.execute(
                "INSERT INTO question_dep(question_id,dep_type,depends_on_question_id,status,created_cycle) "
                "VALUES (1,'question',?,?,1)", (dep_id, status))


def test_dependency_wait_allows_multiple_same_question_deps_and_blocked_escape(env):
    state, compiler = env
    _seed_dependency_wait(state, ["satisfied", "blocked"])
    adv = SqliteAdvancer(state, compiler, _bootstrap_provider, attack=object())

    cyc = adv._resume_or_open()

    assert cyc.route == "attack" and cyc.question_id == "q1"


def test_dependency_wait_stays_blocked_while_any_dep_pending(env):
    state, compiler = env
    _seed_dependency_wait(state, ["satisfied", "pending"])
    adv = SqliteAdvancer(state, compiler, _bootstrap_provider, attack=object())

    assert adv._resume_or_open() is None
    assert "1 个 pending dep" in adv.last_block_reason


# ============ 外层驱动循环 + decompose（reasoning-only：bootstrap→decompose→terminate）============
def _seq_provider(cyc, pack):
    """确定性序列 provider：bootstrap 造根 + 选 decompose；decompose 给活跃根挂两子 + 选 terminate。"""
    if cyc.route == "bootstrap":
        return {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题：EEG 有跨数据集通用规律吗？", "local_key": "root"}]},
                "selection.json": {"next_question_id": "root", "next_intent": "decompose",
                                   "scores": [{"question_id": "root", "score": 0.9, "est_cost": 1.0}]}}
    return {"tree_ops.json": {"ops": [{"op": "add_children", "parent_question_id": cyc.question_id,
                                       "children": [{"text": "子问题1", "local_key": "c1"},
                                                    {"text": "子问题2", "local_key": "c2"}]}]},
            "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}


def _final_state(path):
    """终库确定性状态（**排除 timestamp/attempt_id/log offset 等非确定字段**，§7.1 M3）：cycle 全过程字段
    （含 durable handoff 的 next_question_id/next_intent）+ question（含 visit/score/est_cost）+ question_dep 全列。"""
    c = db.connect(path)
    cycles = c.execute("SELECT id,status,route,next_intent,next_question_id,active_question_id FROM cycle ORDER BY id").fetchall()
    questions = c.execute("SELECT id,parent_id,text,status,visit_count,score,est_cost FROM question ORDER BY id").fetchall()
    deps = c.execute("SELECT question_id,dep_type,depends_on_question_id,depends_on_baseline_id,status "
                     "FROM question_dep ORDER BY id").fetchall()
    c.close()
    return {"cycles": cycles, "questions": questions, "deps": deps}


def test_run_cycles_bootstrap_decompose_terminate(env, tmp_path):
    state, compiler = env
    ids = SqliteAdvancer(state, compiler, _seq_provider).run_cycles(max_cycles=8)
    assert len(ids) == 2                                  # bootstrap + decompose，then terminate 停机
    qs = {q[2]: q for q in state._qall("SELECT id,parent_id,text,status FROM question ORDER BY id")}
    assert "根问题：EEG 有跨数据集通用规律吗？" in {q[2] for q in qs.values()}
    assert "子问题1" in qs and "子问题2" in qs             # decompose 挂了两子
    assert state.last_done_cycle().next_intent == "terminate"


def test_storage_reconcile_failure_never_replays_committed_cycle(env):
    state, compiler = env
    provider_calls = {"n": 0}
    storage_calls = {"n": 0}

    def provider(cyc, pack):
        provider_calls["n"] += 1
        return _seq_provider(cyc, pack)

    def fail_after_first_cycle():
        storage_calls["n"] += 1
        if state.last_done_cycle() is not None:
            raise RuntimeError("snapshot unavailable")

    advancer = SqliteAdvancer(
        state, compiler, provider, storage_reconciler=fail_after_first_cycle)
    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        advancer.run_cycles(max_cycles=1)
    assert state.last_done_cycle().cycle_id == "c1"
    assert provider_calls["n"] == 1

    # 同一进程重入先补 snapshot，随后据已提交 selection 开 c2；绝不重发 c1 provider。
    advancer.storage_reconciler = lambda: None
    advancer.run_cycles(max_cycles=1)
    assert provider_calls["n"] == 2
    assert state.last_done_cycle().cycle_id == "c2"


def test_immediate_abort_is_snapshotted_before_opening_next_cycle(env):
    state, compiler = env
    cycle = _prepared_bootstrap(state)
    storage_calls = {"n": 0}
    aborted = {"done": False}

    def precheck(_cycle):
        if not aborted["done"]:
            state.mark_cycle_done(cycle.cycle_id, "aborted")
            aborted["done"] = True
        return None

    def storage():
        storage_calls["n"] += 1
        if storage_calls["n"] == 2:  # entry no-op 后，abort terminal cut 必须先于 c2 open
            raise RuntimeError("snapshot boundary reached")

    advancer = SqliteAdvancer(
        state, compiler, _seq_provider, precheck=precheck,
        storage_reconciler=storage)
    with pytest.raises(RuntimeError, match="snapshot boundary"):
        advancer.run_cycles(max_cycles=1)
    assert state.daemon.query_one("SELECT count(*) FROM cycle") == (1,)
    assert state.cycle(cycle.cycle_id).status == "aborted"


def test_stage_boundary_abort_plus_pause_is_snapshotted_before_return(env):
    state, compiler = env
    observed = []
    aborted = {"done": False}

    def precheck(cycle):
        if cycle is not None and not aborted["done"]:
            state.mark_cycle_done(cycle.cycle_id, "aborted")
            aborted["done"] = True
            return "pause 指令生效中"
        return None

    def storage():
        terminal = state.daemon.query_one(
            "SELECT id,status FROM cycle WHERE status IN ('done','failed','aborted') "
            "ORDER BY id DESC LIMIT 1")
        observed.append(terminal)

    advancer = SqliteAdvancer(
        state, compiler, _seq_provider, precheck=precheck,
        storage_reconciler=storage)
    assert advancer.run_cycles(max_cycles=1) == []
    assert state.cycle("c1").status == "aborted"
    assert observed[-1] == (1, "aborted")


def test_storage_reconcile_runs_after_durable_round_stop(env):
    state, compiler = env
    events = []

    class StopAfterRound:
        def already_stopped(self):
            return None

        def check_before_round(self):
            return None

        def check_after_round(self):
            events.append("stop")
            return {"reason": "test-stop"}

    def reconcile():
        events.append("snapshot")

    ids = SqliteAdvancer(
        state, compiler, _seq_provider, stop_controller=StopAfterRound(),
        storage_reconciler=reconcile).run_cycles(max_cycles=8)
    assert ids == ["c1"]
    assert events == ["snapshot", "snapshot", "stop", "snapshot"]


def test_reentry_recovers_budget_stop_before_storage_snapshot(env):
    state, compiler = env
    events = []

    class RecoveredStop:
        stopped = False

        def check_before_round(self):
            events.append("stop")
            self.stopped = True
            return {"reason": "budget_exhausted"}

        def already_stopped(self):
            return "budget_exhausted" if self.stopped else None

    def reconcile():
        assert events == ["stop"]
        events.append("snapshot")

    advancer = SqliteAdvancer(
        state, compiler, _seq_provider, stop_controller=RecoveredStop(),
        storage_reconciler=reconcile)
    assert advancer.run_cycles(max_cycles=1) == []
    assert advancer.last_stop_reason == "budget_exhausted"
    assert events == ["stop", "snapshot"]


def test_resumed_import_worker_cycle_is_snapshotted_before_next_research_cycle():
    events = []
    worker_cycle = SimpleNamespace(cycle_id="c7", route=None)
    terminal = SimpleNamespace(cycle_id="c7", route=None, next_intent="terminate")

    class Daemon:
        @staticmethod
        def query_one(_sql, _params=()):
            return (1,)

    class State:
        daemon = Daemon()
        current = worker_cycle

        def inflight_cycle(self):
            return self.current

        @staticmethod
        def pending_goal_amend_directive():
            return None

        @staticmethod
        def last_done_cycle():
            return terminal

    state = State()

    class Worker:
        @staticmethod
        def resume_cycle(cycle):
            assert cycle is worker_cycle
            events.append("worker-terminal")
            state.current = None

    advancer = SqliteAdvancer(
        state, compiler=None, reasoning_provider=lambda _c, _p: None,
        import_worker=Worker(), storage_reconciler=lambda: events.append("snapshot"))
    assert advancer._resume_or_open() is None
    assert events == ["worker-terminal", "snapshot"]


def test_open_boundary_reconciles_after_observing_no_inflight_cycle():
    events = []

    class State:
        @staticmethod
        def inflight_cycle():
            return None

        @staticmethod
        def last_done_cycle():
            return None

        @staticmethod
        def pending_goal_amend_directive():
            return None

        @staticmethod
        def open_or_resume_cycle():
            assert events == ["snapshot"]
            raise RuntimeError("open reached")

    advancer = SqliteAdvancer(
        State(), compiler=None, reasoning_provider=lambda _c, _p: None,
        storage_reconciler=lambda: events.append("snapshot"))
    with pytest.raises(RuntimeError, match="open reached"):
        advancer._resume_or_open()


def test_run_cycles_resume_after_restart(tmp_path):
    """恢复：跑 1 轮（bootstrap）后**换新 Advancer/StateStore（模拟重启）**指向同库 → 续跑 decompose 并停机，
    终库与一次跑完全一致（durable 交接、无进程内记忆）。"""
    path = str(tmp_path / "research.sqlite")

    def _fresh(p):
        daemon = WriteDaemon(db.connect(p))
        st = SQLiteStateStore(daemon, POLICY)
        comp = SqliteCompiler(db.connect(p), POLICY)
        return daemon, st, comp

    # 参照：一次跑完（另一库）
    ref_path = str(tmp_path / "ref.sqlite")
    d0, s0, c0 = _fresh(ref_path); s0.create_goal(text="EEG 通用规律研究", predicate_json={})
    SqliteAdvancer(s0, c0, _seq_provider).run_cycles(max_cycles=8)
    d0.conn.close(); c0.conn.close()

    # 断点跑：先 1 轮（bootstrap），关连接（模拟进程死），再新实例续跑
    d1, s1, c1 = _fresh(path); s1.create_goal(text="EEG 通用规律研究", predicate_json={})
    SqliteAdvancer(s1, c1, _seq_provider).run_cycles(max_cycles=1)   # 只 bootstrap
    d1.conn.close(); c1.conn.close()                                  # 模拟重启：旧连接消失

    d2, s2, c2 = _fresh(path)
    SqliteAdvancer(s2, c2, _seq_provider).run_cycles(max_cycles=8)    # 续跑 → decompose → terminate
    d2.conn.close(); c2.conn.close()

    assert _final_state(path) == _final_state(ref_path)              # 续跑终库 == 一次跑完（排除时间戳字段）


def test_kill9_recovery_final_state_identical(tmp_path):
    """§7.1 M3 恢复验收（**真 kill -9**）：驱动循环进到 decompose 轮（阶段将写未写）时 SIGKILL 子进程 →
    全新进程续跑 → 终库状态与不杀一次跑完**一致**（排除 timestamp/attempt_id/log offset 等非确定字段）。"""
    # 参照：一次跑完（另一库）
    ref = str(tmp_path / "ref.sqlite")
    d0 = WriteDaemon(db.connect(ref)); s0 = SQLiteStateStore(d0, POLICY)
    s0.create_goal(text="EEG 通用规律研究", predicate_json={})
    c0 = SqliteCompiler(db.connect(ref), POLICY)
    SqliteAdvancer(s0, c0, _seq_provider).run_cycles(max_cycles=8)
    d0.conn.close(); c0.conn.close()

    # 断点库：父建 goal + 关连接（子进程独占写）
    path = str(tmp_path / "research.sqlite")
    dp = WriteDaemon(db.connect(path)); SQLiteStateStore(dp, POLICY).create_goal(text="EEG 通用规律研究", predicate_json={})
    dp.conn.close()

    marker = tmp_path / "ready.flag"
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent(f"""
        import sys, time, yaml
        from pathlib import Path
        sys.path.insert(0, {str(SYSTEM_ROOT)!r})
        from orchestrator import database as db
        from orchestrator.statestore_sqlite import SQLiteStateStore
        from orchestrator.compiler_sqlite import SqliteCompiler
        from orchestrator.writedaemon import WriteDaemon
        from orchestrator.advancer import SqliteAdvancer
        POL = yaml.safe_load((Path({str(SYSTEM_ROOT)!r})/"policies"/"policy.yaml").read_text(encoding="utf-8"))
        def prov(cyc, pack):
            if cyc.route == "bootstrap":
                return {{"tree_ops.json": {{"ops":[{{"op":"create_root","text":"根问题：EEG 有跨数据集通用规律吗？","local_key":"root"}}]}},
                        "selection.json": {{"next_question_id":"root","next_intent":"decompose","scores":[{{"question_id":"root","score":0.9,"est_cost":1.0}}]}}}}
            open({str(marker)!r}, "w").close()   # 已进 decompose 轮、阶段将写未写 → 信号父可杀
            time.sleep(60)                        # 挂起等 kill -9（decompose 阶段永不提交）
        s = SQLiteStateStore(WriteDaemon(db.connect({path!r})), POL)
        comp = SqliteCompiler(db.connect({path!r}), POL)
        SqliteAdvancer(s, comp, prov).run_cycles(max_cycles=8)
    """), encoding="utf-8")

    proc = subprocess.Popen([sys.executable, str(worker)])
    try:
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists(), "worker 未到达 decompose 挂起点"
        proc.kill()                               # SIGKILL：不给清理机会
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    # 续跑（全新实例指向同库 = 重启进程语义）→ 正常 provider 完成 decompose→terminate
    d2 = WriteDaemon(db.connect(path)); s2 = SQLiteStateStore(d2, POLICY)
    c2 = SqliteCompiler(db.connect(path), POLICY)
    ids = SqliteAdvancer(s2, c2, _seq_provider).run_cycles(max_cycles=8)
    d2.conn.close(); c2.conn.close()

    assert ids, "续跑应至少推进一轮（decompose）"
    assert _final_state(path) == _final_state(ref)   # 杀后续跑 == 不杀跑完（排除非确定字段）
