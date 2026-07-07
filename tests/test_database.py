"""CP2.1 · 建库与三重锁的否定/正向用例（M1a：schema 落地并证明冻结锁生效）。

覆盖 database.connect / verify_schema 的守卫：
- 正向：新建 :memory: → 36/72/29/1、foreign_keys ON、file 库 WAL、重开幂等（不重跑 migration）。
- 否定：checksum 漂移 / 计数漂移 / 版本不符 → SchemaDriftError；FK 实际生效（悬空引用被拒）。
- 保真：migration body 与 reference 附录 A（行 918–1614）字节一致——护「逐字摘自」claim。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orchestrator import database as db

SYSTEM_ROOT = Path(__file__).resolve().parent.parent            # meta-research/
REFERENCE = SYSTEM_ROOT.parent / "reference" / "第一部分-系统架构设计.md"


# ---- 正向：建库与计数 ------------------------------------------------------
def test_fresh_build_counts_and_version():
    conn = db.connect(":memory:")
    assert db.live_counts(conn) == db.EXPECTED_COUNTS == {
        "table": 36, "trigger": 72, "index": 29, "view": 1}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    # verify_schema 已在 connect 内跑过；再显式跑一次不抛
    db.verify_schema(conn)


def test_foreign_keys_enabled_per_connection():
    conn = db.connect(":memory:")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_foreign_key_actually_enforced():
    """FK ON 不是摆设：插入指向不存在 goal 的 cycle 应被拒。"""
    conn = db.connect(":memory:")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
            "VALUES (1,999,1,'created','v0')")


def test_file_db_uses_wal_and_reopen_is_idempotent(tmp_path):
    path = tmp_path / "research.sqlite"
    conn = db.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    # 落一行可辨识数据，关闭后重开——不得重跑 migration（会撞 UNIQUE / 覆盖）、须过校验
    conn.execute("INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}')")
    conn.commit()
    conn.close()

    reopened = db.connect(path)                # fresh=False 分支
    db.verify_schema(reopened)
    assert reopened.execute("SELECT text FROM goal WHERE id=1").fetchone()[0] == "g"
    assert db.live_counts(reopened) == db.EXPECTED_COUNTS


# ---- 否定：三重锁 ----------------------------------------------------------
def test_checksum_drift_rejected(monkeypatch):
    """migration 文件与冻结锚不符（此处反向模拟：改锚常量）→ 建库被拒。"""
    monkeypatch.setattr(db, "MIGRATION_SHA256", "0" * 64)
    with pytest.raises(db.SchemaDriftError, match="checksum 漂移"):
        db.connect(":memory:")


def test_count_drift_rejected():
    """库内实际计数与 36/72/29/1 不符 → verify_schema 拒。"""
    conn = db.connect(":memory:")
    conn.execute("DROP TRIGGER trg_goal_nodel")           # 72 → 71
    with pytest.raises(db.SchemaDriftError, match="计数漂移"):
        db.verify_schema(conn)


def test_user_version_mismatch_rejected():
    conn = db.connect(":memory:")
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(db.SchemaDriftError, match="版本不符"):
        db.verify_schema(conn)


def test_reopen_rejects_checksum_drift(tmp_path, monkeypatch):
    """字节冻结须在主运行路径生效：既有库 reopen 时若 DDL 文件漂移也应被拒（非仅 fresh）。"""
    path = tmp_path / "research.sqlite"
    db.connect(path).close()                              # 正常建库
    monkeypatch.setattr(db, "MIGRATION_SHA256", "0" * 64)  # 模拟文件漂移
    with pytest.raises(db.SchemaDriftError, match="checksum 漂移"):
        db.connect(path)                                 # reopen 也必须拒


def test_analyze_does_not_break_counts(tmp_path):
    """运行库跑过 ANALYZE 会生成 sqlite_stat* 内部表；计数口径须只数用户对象、不误判漂移。"""
    path = tmp_path / "research.sqlite"
    conn = db.connect(path)
    conn.execute("ANALYZE")                               # 生成 sqlite_stat1
    conn.commit()
    assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='sqlite_stat1'").fetchone()[0] == 1
    db.verify_schema(conn)                                # 仍须过（36 表不含 sqlite_stat1）
    assert db.live_counts(conn) == db.EXPECTED_COUNTS


# ---- 保真：migration 逐字摘自 reference 附录 A ------------------------------
@pytest.mark.skipif(not REFERENCE.exists(), reason="reference 文档不在本检出中")
def test_migration_matches_reference_appendix_a():
    """护「逐字摘自附录 A」：migration 去掉 4 行治理头 == reference 行 918–1614。

    checksum 只锁「文件未变」，本用例锁「文件仍等于规范源」——两者互补。
    """
    ref_lines = REFERENCE.read_text(encoding="utf-8").splitlines()
    ref_ddl = "\n".join(ref_lines[917:1614])              # 1-based 918..1614 → 0-based 917..1613
    mig_lines = db.MIGRATION_FILE.read_text(encoding="utf-8").splitlines()
    mig_body = "\n".join(mig_lines[4:])                    # 去掉 4 行 -- 治理头
    assert mig_body == ref_ddl
