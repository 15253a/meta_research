"""CP3.3 · 运行观测摘要段进 reasoning 固定锚（§4.7）+ 门禁 authorizer 拒读负例（§3.1.2 隔离）。

核心验收（§7.1 M2）：
- 观测摘要从 execution_observation 渲进 reasoning 锚点（机器事实，不塞全量 log）；确定性（同快照字节一致）。
- **摘要进 pack 但不进 gate 判据**：编译器普通只读连接读得到 execution_observation，门禁 authorizer 拒读同表。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator.compiler_sqlite import SqliteCompiler
from orchestrator.gate_sqlite import open_gate_read_conn

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _obs_seed(conn):
    """seed_minimal（cycle1 reasoning，run1=build/success）+ 本轮 train 日志 + 两条观测（parser 机器事实 / codex 仅 digest）。"""
    conftest.seed_minimal(conn)
    conn.executescript("""
      INSERT INTO execution_log(id,run_id,cycle_id,log_kind,ref,content_hash) VALUES (1,1,1,'train','p/t.log','h');
      INSERT INTO execution_observation(id,execution_log_id,source,nan_seen,divergence_flag,oom_count,
                                        warning_count,retry_count,last_loss,loss_trend,wall_clock_sec)
        VALUES (1,1,'parser',0,0,0,2,1,0.12,'down',123.5);
      INSERT INTO execution_observation(id,execution_log_id,source,digest_ref,digest_hash)
        VALUES (2,1,'codex','digest/ref.md','dh');
    """)
    conn.commit()


def _comp(conn):
    return SqliteCompiler(conn, POLICY)


@pytest.fixture()
def comp():
    c = db.connect(":memory:")
    _obs_seed(c)
    return _comp(c)


# ============ 观测摘要渲染（§4.7）============
def test_observation_renders_machine_facts(comp):
    a = comp.render(cycle_id="c1", stage="reasoning").anchor_md
    assert "本轮运行观测摘要" in a
    assert "loss_trend=down" in a and "wall_clock_sec=123.5" in a
    assert "nan=0" in a and "warning=2" in a and "retry=1" in a and "last_loss=0.12" in a


def test_observation_iron_law_header(comp):
    """铁律声明进 header：观测不得作 novelty/success/correctness/关问题选择输入（§3.1.2 防绕过门禁）。"""
    a = comp.render(cycle_id="c1", stage="reasoning").anchor_md
    assert "不得作" in a and "novelty" in a and "关问题" in a


def test_observation_codex_row_digest_only(comp):
    """codex source 行只渲 digest_ref、不冒充机器事实（DDL CHECK 保证机器列 NULL）。"""
    a = comp.render(cycle_id="c1", stage="reasoning").anchor_md
    assert "codex 摘要" in a and "digest/ref.md" in a
    # codex 那行不带 nan=/loss_trend= 机器字段（parser 行才有）
    codex_line = [ln for ln in a.splitlines() if "obs2" in ln][0]
    assert "nan=" not in codex_line and "loss_trend=" not in codex_line


def test_observation_deterministic(comp):
    """同快照两渲字节一致（含观测段）。"""
    p1 = comp.render(cycle_id="c1", stage="reasoning")
    p2 = comp.render(cycle_id="c1", stage="reasoning")
    assert p1.pack_hash == p2.pack_hash and p1.anchor_md == p2.anchor_md


def test_observation_excludes_created_at(comp):
    """确定性纪律：观测行**不渲** created_at（DB 插入 wall-clock；两次真实运行会不同 → 破字节一致）。"""
    created_at = comp.conn.execute("SELECT created_at FROM execution_observation WHERE id=1").fetchone()[0]
    a = comp.render(cycle_id="c1", stage="reasoning").anchor_md
    assert created_at not in a


def test_no_observation_renders_honest():
    """本轮无观测 → 诚实占位（不臆造），段仍在。"""
    c = db.connect(":memory:")
    conftest.seed_minimal(c)   # 无 execution_observation
    a = _comp(c).render(cycle_id="c1", stage="reasoning").anchor_md
    assert "本轮运行观测摘要" in a and "（本轮无运行观测）" in a


# ============ 门禁 authorizer 拒读负例（摘要进 pack 但不进 gate 判据）============
def test_observation_in_pack_but_denied_to_gate(tmp_path):
    """§3.1.2 隔离：同一 execution_observation——编译器普通只读连接读得到、渲进 pack；
    门禁 authorizer 只读连接**拒读**（证观测影响 reasoning 但绝不进 gate 判据；须文件库，门禁独立连接）。"""
    path = str(tmp_path / "research.sqlite")
    seed = db.connect(path); _obs_seed(seed); seed.close()

    pack = _comp(db.connect(path)).render(cycle_id="c1", stage="reasoning")
    assert "loss_trend=down" in pack.anchor_md          # 编译器读到观测、渲进 pack

    read = open_gate_read_conn(path)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|prohibited"):
        read.execute("SELECT count(*) FROM execution_observation").fetchone()   # 门禁拒读同表
