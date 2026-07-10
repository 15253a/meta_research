"""SQLite 唯一真相的建库与守卫（M1a：migration version / schema checksum 锁定）。

治理（详 db/README.md）：
- `db/migrations/0001_appendix_a.sql` 逐字摘自《第一部分》附录 A，**字节冻结**——
  本模块以 SHA256 常量锁定；文件被改动（漂移/手滑）即拒绝建库。
- 目标计数锁定：36 表 / 72 触发器 / 29 索引（12 显式 + 17 UNIQUE 自动）/ 1 视图。
- PRAGMA user_version = schema 版本（=1）；不建额外元数据表（36 表计数是验收对象）。

连接纪律：foreign_keys 每连接显式开启（SQLite 默认关）；文件库开 WAL（§6.2）。
写连接唯一性由上层（WriteDaemon/驱动器单进程）保证，本模块只管建库与校验。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Dict, Union

MIGRATION_FILE = Path(__file__).resolve().parent.parent / "db" / "migrations" / "0001_appendix_a.sql"

# 字节冻结锚：附录 A 提取文件的 SHA256。任何改动=决策性改动，须走检查点评审并同步更新此常量。
MIGRATION_SHA256 = "c56df2db0434877b5b3dcba17302e8967ed256a337988d0dd58cd5c7e5cfffd4"

SCHEMA_VERSION = 1

# 附录 A 自述计数（索引口径含 UNIQUE 自动索引，《第一部分》§5.7）
EXPECTED_COUNTS: Dict[str, int] = {"table": 36, "trigger": 72, "index": 29, "view": 1}


class SchemaDriftError(RuntimeError):
    """migration 文件或库内 schema 与冻结锚不符（M1a 验收：checksum/计数锁定）。"""


def _read_migration() -> str:
    data = MIGRATION_FILE.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != MIGRATION_SHA256:
        raise SchemaDriftError(
            f"migration 文件 checksum 漂移：{digest} ≠ 冻结锚 {MIGRATION_SHA256}（{MIGRATION_FILE}）")
    return data.decode("utf-8")


def live_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    # 排除全部 sqlite_ 内部对象（sqlite_stat1/4 by ANALYZE、sqlite_sequence、sqlite_autoindex%…）——
    # 只数用户 schema 对象，否则运行库跑过 ANALYZE / PRAGMA optimize 会凭空多出 sqlite_stat* 被误判漂移。
    rows = conn.execute(
        "SELECT type, count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' GROUP BY type"
    ).fetchall()
    counts = {t: n for t, n in rows}
    auto = conn.execute(   # UNIQUE 自动索引单独计入 index 口径（附录 A 的 29 含 17 个 sqlite_autoindex%）
        "SELECT count(*) FROM sqlite_master WHERE type='index' AND name LIKE 'sqlite_autoindex%'"
    ).fetchone()[0]
    return {
        "table": counts.get("table", 0),
        "trigger": counts.get("trigger", 0),
        "index": counts.get("index", 0) + auto,   # 口径含 UNIQUE 自动索引
        "view": counts.get("view", 0),
    }


def verify_schema(conn: sqlite3.Connection) -> None:
    got = live_counts(conn)
    if got != EXPECTED_COUNTS:
        raise SchemaDriftError(f"schema 计数漂移：{got} ≠ {EXPECTED_COUNTS}")
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver != SCHEMA_VERSION:
        raise SchemaDriftError(f"schema 版本不符：user_version={ver} ≠ {SCHEMA_VERSION}")


def connect(path: Union[str, Path] = ":memory:") -> sqlite3.Connection:
    """建库（新库执行冻结 migration）或打开既有库，并做 checksum/计数/版本三重校验。

    checksum 每次 connect 都校验（fresh 与 reopen 一致）：字节冻结须在**主运行路径**上生效——
    research.sqlite 每进程启动都是 reopen，若只在 fresh 校验，DDL 文件漂移后既有库仍照常打开，
    冻结形同虚设。故这里先无条件校验文件、再决定建库还是仅开库。
    """
    is_memory = str(path) == ":memory:"
    migration_sql = _read_migration()   # 无条件校验文件 checksum（漂移即在此抛 SchemaDriftError）
    # The run process has one writer connection, but the interaction pump and
    # research driver use it from different threads.  WriteDaemon serializes
    # every access; disabling sqlite's thread-affinity check is therefore safe
    # and avoids opening a second writer connection.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    fresh = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    if fresh:
        conn.executescript(migration_sql)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    if not is_memory:
        conn.execute("PRAGMA journal_mode = WAL")   # 只读连接与单写互不阻塞（§6.2）
    verify_schema(conn)
    return conn
