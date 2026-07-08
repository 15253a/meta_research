"""CP4.1 · SqliteAdvancer（M3：derive_next_route 全矩阵 + advance bootstrap 创世轮 + 阶段原子/幂等）。

核心：
- derive_next_route 忠实 §6.13(3) 矩阵（所有 intent×outcome 行）。
- advance 驱动 bootstrap 轮：单一 atomic 事务落 create_root + selection + mark_done（真 SQLite）。
- **恢复语义前身**：阶段内失败 → 整事务回滚（create_root 亦回滚），cycle 停在阶段前状态；已终态 → 幂等 done。
"""
from __future__ import annotations

from pathlib import Path

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


# ============ advance bootstrap 创世轮（真 SQLite）============
@pytest.fixture()
def env(tmp_path):
    """真组件：WriteDaemon + SQLiteStateStore（写）+ SqliteCompiler（独立读连接，须文件库共享）。"""
    path = str(tmp_path / "research.sqlite")
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="EEG 跨数据集通用规律研究", predicate_json={})
    compiler = SqliteCompiler(db.connect(path), POLICY, goal_body_md="EEG 跨数据集通用规律研究")
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


def test_advance_non_bootstrap_not_implemented(env):
    """CP4.1 仅 bootstrap；attack/decompose route → NotImplementedError（诚实拒，后续检查点接）。"""
    state, compiler = env
    cyc = _prepared_bootstrap(state, route="attack")
    adv = SqliteAdvancer(state, compiler, _bootstrap_provider)
    with pytest.raises(NotImplementedError, match="bootstrap"):
        adv.advance(cyc.cycle_id)
