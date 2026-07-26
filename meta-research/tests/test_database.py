"""SQLite schema chain: frozen v1 plus hash-locked additive migrations.

覆盖 database.connect / verify_schema 的守卫：
- 正向：fresh 走完整 migration chain；v1 文件库自动升级；foreign_keys ON；
  本地 file 库 WAL、共享库 rollback、重开幂等。
- 否定：checksum 漂移 / 计数漂移 / 版本不符 → SchemaDriftError；FK 实际生效（悬空引用被拒）。
- 保真：0001 body 与 reference 附录 A（行 918–1614）字节一致且 SHA 不变。
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import pytest

from orchestrator import database as db

SYSTEM_ROOT = Path(__file__).resolve().parent.parent            # meta-research/
REFERENCE = SYSTEM_ROOT.parent / "reference" / "第一部分-系统架构设计.md"


def test_migration_locator_supports_wheel_share_layout(tmp_path, monkeypatch):
    package = tmp_path / "site" / "orchestrator"
    package.mkdir(parents=True)
    installed = (
        tmp_path / "site" / "share" / "meta-research"
        / "db" / "migrations" / "0001_appendix_a.sql")
    installed.parent.mkdir(parents=True)
    installed.write_text("-- installed migration\n", encoding="utf-8")
    monkeypatch.delenv("META_RESEARCH_SYSTEM_ROOT", raising=False)
    monkeypatch.setattr(db, "__file__", str(package / "database.py"))
    monkeypatch.setattr(db.sysconfig, "get_path", lambda _name: str(tmp_path / "other"))
    assert db._resolve_migration_file() == installed.resolve()


# ---- 正向：建库与计数 ------------------------------------------------------
def test_fresh_build_counts_and_version():
    conn = db.connect(":memory:")
    assert db.live_counts(conn) == db.EXPECTED_COUNTS == {
        "table": 46, "trigger": 88, "index": 50, "view": 1}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    # verify_schema 已在 connect 内跑过；再显式跑一次不抛
    db.verify_schema(conn)


def test_fresh_build_contains_bundle_dag_tables():
    conn = db.connect(":memory:")
    names = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "bundle_target_node",
        "bundle_target_dependency",
        "bundle_target_admission",
        "bundle_source_request",
        "bundle_source_binding",
        "bundle_worker_task",
        "bundle_scheduler_state",
        "bundle_resource_request",
        "bundle_resource_lease",
        "bundle_terminal_report",
    } <= names


def test_existing_v1_file_is_upgraded_once_without_losing_data(
        tmp_path, monkeypatch):
    path = tmp_path / "research.sqlite"
    legacy = sqlite3.connect(path)
    legacy.executescript(db._read_migration())
    legacy.execute("PRAGMA user_version = 1")
    legacy.execute(
        "INSERT INTO goal(id,version,text,predicate_json) "
        "VALUES (1,1,'legacy','{}')")
    legacy.commit()
    legacy.close()

    monkeypatch.setattr(db, "filesystem_type_for_path", lambda _path: "gpfs")
    upgraded = db.connect(path)
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 2
    assert upgraded.execute(
        "SELECT text FROM goal WHERE id=1").fetchone()[0] == "legacy"
    assert upgraded.execute(
        "SELECT count(*) FROM sqlite_master "
        "WHERE type='table' AND name='bundle_target_admission'"
    ).fetchone()[0] == 1
    upgraded.close()

    reopened = db.connect(path)
    assert reopened.execute("PRAGMA user_version").fetchone()[0] == 2
    assert reopened.execute(
        "SELECT text FROM goal WHERE id=1").fetchone()[0] == "legacy"


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


def test_local_file_db_uses_wal_and_reopen_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "research.sqlite"
    monkeypatch.setattr(db, "filesystem_type_for_path", lambda _path: "ext4")
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


@pytest.mark.parametrize("fstype", ["gpfs", "nfs4", "cifs", None, "futurefs"])
def test_shared_or_unknown_filesystem_uses_rollback_journal(tmp_path, monkeypatch, fstype):
    path = tmp_path / "research.sqlite"
    monkeypatch.setattr(db, "filesystem_type_for_path", lambda _path: fstype)
    conn = db.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    conn.close()


def test_mountinfo_parser_prefers_deepest_boundary_and_decodes_escape():
    raw = (
        "1 0 0:1 / / rw - ext4 root rw\n"
        "2 1 0:2 / /share rw - xfs local rw\n"
        "3 1 0:3 / /shared rw - gpfs vepfs rw\n"
        "4 1 0:4 / /mnt/data\\040set rw - gpfs escaped rw\n"
    ).encode("utf-8")
    assert db._filesystem_type_from_mountinfo("/shared/db.sqlite", raw) == "gpfs"
    assert db._filesystem_type_from_mountinfo("/share/db.sqlite", raw) == "xfs"
    assert db._filesystem_type_from_mountinfo("/share-other/db.sqlite", raw) == "ext4"
    assert db._filesystem_type_from_mountinfo("/mnt/data set/db.sqlite", raw) == "gpfs"


def test_mountinfo_conflicting_stacked_mount_fails_safe():
    raw = (
        "1 0 0:1 / / rw - ext4 root rw\n"
        "2 1 0:2 / /shared rw - ext4 lower rw\n"
        "3 1 0:3 / /shared rw - gpfs upper rw\n"
    ).encode("utf-8")
    assert db._filesystem_type_from_mountinfo("/shared/db.sqlite", raw) is None
    assert db.journal_mode_for_filesystem(None) == "delete"


def test_filesystem_detection_resolves_alias_before_mount_lookup(monkeypatch):
    raw = (
        "1 0 0:1 / / rw - ext4 root rw\n"
        "2 1 0:2 / /shared rw - gpfs vepfs rw\n"
    ).encode("utf-8")
    monkeypatch.setattr(db.os.path, "realpath", lambda _path: "/shared/research.sqlite")
    monkeypatch.setattr(db, "_read_mountinfo", lambda: raw)
    assert db.filesystem_type_for_path("/local-looking/alias.sqlite") == "gpfs"


def test_gpfs_reopen_recovers_committed_wal_then_switches_to_delete(
        tmp_path, monkeypatch):
    """Crash residue from the old WAL contract must be adopted without data loss."""
    path = tmp_path / "research.sqlite"
    monkeypatch.setattr(db, "filesystem_type_for_path", lambda _path: "ext4")
    conn = db.connect(path)
    conn.close()

    child = os.fork()
    if child == 0:  # pragma: no cover - parent verifies the durable outcome
        try:
            writer = sqlite3.connect(path)
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'wal-crash','{}')")
            writer.commit()
            os._exit(0)
        except BaseException:
            os._exit(3)
    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert Path(str(path) + "-wal").exists()
    stale_shm = Path(str(path) + "-shm")
    assert stale_shm.exists()
    stale_marker = b"STALE-SHM-MUST-NOT-BE-USED"
    stale_shm.write_bytes(stale_marker)

    monkeypatch.setattr(db, "filesystem_type_for_path", lambda _path: "gpfs")
    statements = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        traced = real_connect(*args, **kwargs)
        traced.set_trace_callback(statements.append)
        return traced

    monkeypatch.setattr(db.sqlite3, "connect", traced_connect)
    recovered = db.connect(path)
    assert recovered.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert recovered.execute("SELECT text FROM goal WHERE id=1").fetchone()[0] == "wal-crash"
    recovered.close()
    assert not Path(str(path) + "-wal").exists()
    assert not stale_shm.exists() or stale_shm.read_bytes() == stale_marker
    normalized = [re.sub(r"\s+", " ", statement.strip()).upper()
                  for statement in statements]
    exclusive = next(i for i, statement in enumerate(normalized)
                     if statement == "PRAGMA LOCKING_MODE = EXCLUSIVE")
    switch = next(i for i, statement in enumerate(normalized)
                  if statement == "PRAGMA JOURNAL_MODE = DELETE")
    normal = next(i for i, statement in enumerate(normalized)
                  if statement == "PRAGMA LOCKING_MODE = NORMAL")
    schema_read = next(i for i, statement in enumerate(normalized)
                       if statement.startswith("SELECT COUNT(*) FROM SQLITE_MASTER"))
    assert exclusive == 0
    assert exclusive < switch < normal < schema_read


def test_live_repo_path_is_detected_as_gpfs():
    mount_path = Path(__file__).resolve()
    if "/vepfs-" not in str(mount_path):
        pytest.skip("workspace is not on the target VEPFS mount")
    assert db.filesystem_type_for_path(mount_path) == "gpfs"
    assert db.journal_mode_for_path(mount_path) == "delete"


# ---- 否定：三重锁 ----------------------------------------------------------
def test_checksum_drift_rejected(monkeypatch):
    """migration 文件与冻结锚不符（此处反向模拟：改锚常量）→ 建库被拒。"""
    monkeypatch.setattr(db, "MIGRATION_SHA256", "0" * 64)
    with pytest.raises(db.SchemaDriftError, match="checksum 漂移"):
        db.connect(":memory:")


def test_additive_migration_checksum_drift_rejected(monkeypatch):
    monkeypatch.setattr(db, "BUNDLE_DAG_MIGRATION_SHA256", "0" * 64)
    with pytest.raises(db.SchemaDriftError, match="checksum 漂移"):
        db.connect(":memory:")


def test_count_drift_rejected():
    """库内实际计数与当前版本不符 → verify_schema 拒。"""
    conn = db.connect(":memory:")
    conn.execute("DROP TRIGGER trg_goal_nodel")
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
