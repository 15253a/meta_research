"""CP3.1 · SqliteCompiler（M2：DB→确定性四区 context_pack）。

核心验收：**同快照+配方+预算+target → 字节一致（diff=0）**。另验四区结构 + applicability 徽标（六枚举确定性规则）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator.compiler_sqlite import SqliteCompiler

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _seed(conn):
    conftest.seed_minimal(conn)   # goal1/cycle1(reasoning)/q1(answered,a1)/池对象
    conn.executescript("""
      INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (2,1,1,1,'q2 开放','open','agent');
      INSERT INTO question(id,parent_id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (3,2,1,1,1,'q3 子','active','decompose');
      UPDATE cycle SET active_question_id=3, route='attack' WHERE id=1;
      INSERT INTO answer_applicability(answer_id,goal_id,goal_ver,status,rationale_md) VALUES (1,1,1,'still_applicable','ok');
      INSERT INTO idea(id,question_id,cycle_id,content_md,status) VALUES (1,3,1,'idea A','candidate');
    """)
    conn.commit()


@pytest.fixture()
def comp():
    conn = db.connect(":memory:")
    _seed(conn)
    return SqliteCompiler(conn, POLICY, goal_body_md="目标全文正文（跨数据集 EEG 通用规律）")


def _bytes(pack):
    return (pack.anchor_md + "\x00" + pack.neighborhood_md + "\x00" + pack.retrieval_md).encode("utf-8")


# ============ 字节一致（M2 核心验收）============
@pytest.mark.parametrize("stage", ["idea", "plan", "bundle", "reasoning"])
def test_render_byte_identical(comp, stage):
    """同快照+配方+预算+target 连渲两次 → pack_hash 与四区字节完全一致（diff=0）。"""
    tid = "t1" if stage == "bundle" else None      # bundle 须逐 target
    p1 = comp.render(cycle_id="c1", stage=stage, target_id=tid)
    p2 = comp.render(cycle_id="c1", stage=stage, target_id=tid)
    assert p1.pack_hash == p2.pack_hash
    assert _bytes(p1) == _bytes(p2)
    assert comp.manifest(p1) == comp.manifest(p2)   # 来源清单亦确定


def test_bundle_requires_target_id(comp):
    with pytest.raises(ValueError, match="target_id 不可为 None"):
        comp.render(cycle_id="c1", stage="bundle")


def test_open_set_scoped_to_goal(comp):
    """codex BLOCKER 回归：可调度集限本 goal——别 goal 的 open 问题不入本 goal 的 reasoning pack。"""
    comp.conn.execute("BEGIN")
    comp.conn.execute("INSERT INTO goal(id,version,text,predicate_json) VALUES (2,1,'g2','{}')")
    comp.conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                      "VALUES (99,2,1,1,'别 goal 的问题','open','agent')")
    comp.conn.execute("COMMIT")
    p = comp.render(cycle_id="c1", stage="reasoning")   # cycle 1 属 goal 1
    assert "别 goal 的问题" not in p.anchor_md


def test_different_stage_different_pack(comp):
    """不同 stage → 不同 pack（确定性不等于恒等）。"""
    assert comp.render(cycle_id="c1", stage="idea").pack_hash != comp.render(cycle_id="c1", stage="reasoning").pack_hash


def test_render_missing_cycle(comp):
    with pytest.raises(ValueError, match="cycle 不存在"):
        comp.render(cycle_id="c999", stage="reasoning")


# ============ 四区结构 ============
def test_reasoning_four_regions(comp):
    p = comp.render(cycle_id="c1", stage="reasoning")
    assert "route=attack" in p.anchor_md and "目标全文" in p.anchor_md
    assert "本轮问题卡 Qn" in p.anchor_md and "q3" in p.anchor_md      # active question=q3
    assert "祖先链" in p.neighborhood_md and "q2" in p.neighborhood_md  # q3 的父 q2
    assert p.retrieval_md == "" and p.refs == []                        # 检索/引用区 CP3.2 填
    assert "采集打分参数" in p.anchor_md


def test_pack_hash_covers_all_regions(comp):
    import hashlib, json
    p = comp.render(cycle_id="c1", stage="reasoning")
    assert p.pack_hash == hashlib.sha256(("\x00".join(
        (p.anchor_md, p.neighborhood_md, p.retrieval_md, json.dumps(p.refs, ensure_ascii=False)))
    ).encode("utf-8")).hexdigest()   # 四区（含 refs）全纳入


def test_manifest_is_pure_function_of_pack(comp):
    """内审 BLOCKER 回归：manifest(pack) 只依赖 pack（按 pack_hash 取 sources），
    中间穿插别的 render 也不串——旧 pack 的 manifest 仍是旧 pack 的来源。"""
    p_idea = comp.render(cycle_id="c1", stage="idea")
    m_idea = comp.manifest(p_idea)
    comp.render(cycle_id="c1", stage="reasoning")        # 穿插一次不同 render
    assert comp.manifest(p_idea) == m_idea               # 旧 pack 的 manifest 不被后来的 render 污染
    assert m_idea["stage"] == "idea" and "policy:acquisition" not in m_idea["sources"]   # 不是 reasoning 的来源


def test_bundle_target_id_consumed(comp):
    """不同 target → 不同 bundle pack（target_id 已消费，非死参）。"""
    p1 = comp.render(cycle_id="c1", stage="bundle", target_id="t1")
    p2 = comp.render(cycle_id="c1", stage="bundle", target_id="t2")
    assert p1.pack_hash != p2.pack_hash and "t1" in p1.anchor_md and "t2" in p2.anchor_md


# ============ applicability 徽标（编译器确定性规则）============
def test_applicability_badge_rendered(comp):
    """已关闭结论处 join answer_applicability → 渲染徽标（此处 still_applicable）。"""
    p = comp.render(cycle_id="c1", stage="reasoning")
    assert "[applicability: still_applicable]" in p.anchor_md


def test_no_applicability_row_no_badge(comp):
    """无 applicability 行 = 无徽标、不占额度。"""
    # 删掉 a1 的 applicability 行后，已关闭结论行不带徽标
    comp.conn.execute("DELETE FROM answer_applicability WHERE answer_id=1")   # 直接改（测试连接）
    comp.conn.commit()
    p = comp.render(cycle_id="c1", stage="reasoning")
    assert "applicability:" not in p.anchor_md
    assert "a1（q1 answered）:" in p.anchor_md   # 结论仍在、只是无徽标


def test_needs_revalidation_badge_shows_spawned(comp):
    """needs_revalidation → 附回看题 QN(状态)（六枚举全渲染）。"""
    comp.conn.executescript("""
      INSERT INTO question(id,parent_id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (4,1,1,1,1,'回看','open','revalidate');
      UPDATE answer_applicability SET status='needs_revalidation', spawned_question_id=4 WHERE answer_id=1;
    """)
    comp.conn.commit()
    p = comp.render(cycle_id="c1", stage="reasoning")
    assert "[applicability: needs_revalidation→q4(open)]" in p.anchor_md


# ============ 开放集 / 祖先链 ============
def test_open_set_ordered_excludes_pending_dep(comp):
    p = comp.render(cycle_id="c1", stage="reasoning")
    assert "q2 开放" in p.anchor_md          # q2 open 且无 pending dep → 在可调度集
    # 给 q2 加 pending dep 后应被排除
    comp.conn.execute("INSERT INTO question_dep(question_id,dep_type,depends_on_question_id,status) VALUES (2,'question',1,'pending')")
    comp.conn.commit()
    p2 = comp.render(cycle_id="c1", stage="reasoning")
    assert "q2 开放" not in p2.anchor_md.split("可调度问题集")[1]
