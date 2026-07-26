"""SQLite 唯一真相的建库、增量迁移与守卫。

治理（详 db/README.md）：
- `db/migrations/0001_appendix_a.sql` 逐字摘自《第一部分》附录 A，**字节冻结**——
  本模块以 SHA256 常量锁定；文件被改动（漂移/手滑）即拒绝建库。
- 后续 schema 只能通过独立、同样 hash-locked 的 additive migration 引入；既有 v1
  文件库按版本逐步升级，fresh 库依次执行完整 migration 链。
- 每个版本同时锁定 schema 对象计数与 ``PRAGMA user_version``。不建 migration
  元数据表，版本链与 checksum 锚由本模块维护。

连接纪律：foreign_keys 每连接显式开启（SQLite 默认关）。本地文件系统用 WAL（§6.2）；
GPFS/未知共享文件系统用 rollback journal。SQLite 官方要求 WAL 的全部进程位于同一 host，
一次共享盘 canary 不能把不受支持的跨 host WAL 变成可靠合同。写连接唯一性仍由上层
InstanceLease + WriteDaemon 保证，本模块只管建库、存储模式与 schema 校验。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sysconfig
from pathlib import Path
from typing import Dict, Optional, Union


_MIGRATION_RELATIVE = Path("db/migrations/0001_appendix_a.sql")
_BUNDLE_DAG_MIGRATION_RELATIVE = Path(
    "db/migrations/0002_bundle_target_dag.sql")


def _resolve_runtime_asset(relative: Path) -> Path:
    """Locate one runtime asset in a checkout or an installed wheel.

    Runtime assets are installed under ``<data>/share/meta-research`` while a
    source checkout keeps them beside the Python package.  Database import
    happens before ``web_app.main`` can pass its resolved system root, so the
    migration locator must independently support the same layouts.
    """
    package_parent = Path(__file__).resolve().parent.parent
    configured = os.environ.get("META_RESEARCH_SYSTEM_ROOT")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([
        package_parent,
        package_parent / "share" / "meta-research",
        Path(sysconfig.get_path("data")) / "share" / "meta-research",
    ])
    for root in candidates:
        try:
            candidate = (root / relative).resolve(strict=True)
        except OSError:
            continue
        if candidate.is_file():
            return candidate
    # Keep import side-effect free and let the existing schema-drift/opening
    # boundary report the missing frozen migration when it is actually used.
    return package_parent / relative


def _resolve_migration_file() -> Path:
    """Backward-compatible locator for the byte-frozen v1 migration."""
    return _resolve_runtime_asset(_MIGRATION_RELATIVE)


MIGRATION_FILE = _resolve_migration_file()
BUNDLE_DAG_MIGRATION_FILE = _resolve_runtime_asset(
    _BUNDLE_DAG_MIGRATION_RELATIVE)

# 字节冻结锚：附录 A 提取文件的 SHA256。任何改动=决策性改动，须走检查点评审并同步更新此常量。
MIGRATION_SHA256 = "c56df2db0434877b5b3dcba17302e8967ed256a337988d0dd58cd5c7e5cfffd4"
BUNDLE_DAG_MIGRATION_SHA256 = (
    "5f2add9dcd5d6fbeb3c870fa677beccf175a472259fcadba2a48af50606b24aa")

SCHEMA_VERSION = 2

# 每版对象计数（索引口径含 UNIQUE 自动索引，《第一部分》§5.7）。
EXPECTED_COUNTS_BY_VERSION: Dict[int, Dict[str, int]] = {
    1: {"table": 36, "trigger": 72, "index": 29, "view": 1},
    2: {"table": 46, "trigger": 88, "index": 50, "view": 1},
}
EXPECTED_COUNTS: Dict[str, int] = EXPECTED_COUNTS_BY_VERSION[SCHEMA_VERSION]


class SchemaDriftError(RuntimeError):
    """migration 文件或库内 schema 与冻结锚不符（M1a 验收：checksum/计数锁定）。"""


class SQLiteStorageModeError(RuntimeError):
    """The live filesystem cannot establish the required SQLite journal mode."""


_LOCAL_WAL_FILESYSTEMS = frozenset({
    "btrfs", "ext2", "ext3", "ext4", "f2fs", "overlay", "overlayfs",
    "ramfs", "tmpfs", "xfs", "zfs",
})
_MOUNTINFO_MAX_BYTES = 4 * 1024 * 1024


def _mountinfo_unescape(value: str) -> str:
    return (value.replace("\\040", " ").replace("\\011", "\t")
            .replace("\\012", "\n").replace("\\134", "\\"))


def _read_mountinfo() -> Optional[bytes]:
    try:
        with open("/proc/self/mountinfo", "rb") as stream:
            raw = stream.read(_MOUNTINFO_MAX_BYTES + 1)
    except OSError:
        return None
    return raw if len(raw) <= _MOUNTINFO_MAX_BYTES else None


def _filesystem_type_from_mountinfo(target: str, raw: bytes) -> Optional[str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    candidates = []
    for line in text.splitlines():
        try:
            left, right = line.split(" - ", 1)
            fields = left.split()
            trailing = right.split()
            mount_point = os.path.normpath(_mountinfo_unescape(fields[4]))
            fstype = trailing[0]
        except (IndexError, ValueError):
            return None
        if (target == mount_point
                or target.startswith(mount_point.rstrip("/") + "/")):
            candidates.append((len(mount_point), fstype))
    if not candidates:
        return None
    deepest = max(depth for depth, _fstype in candidates)
    matches = {fstype for depth, fstype in candidates if depth == deepest}
    return matches.pop() if len(matches) == 1 else None


def filesystem_type_for_path(path: Union[str, Path]) -> Optional[str]:
    """Return the deepest Linux mount fstype, or ``None`` to fail safe.

    The path need not exist yet. Existing symlink components are resolved before
    mount containment so a local-looking alias cannot select WAL for a shared
    target. Linux is already a runtime requirement for the lease/capability layer.
    """
    try:
        target = os.path.normpath(os.path.realpath(os.path.abspath(os.fspath(path))))
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    raw = _read_mountinfo()
    if raw is None:
        return None
    return _filesystem_type_from_mountinfo(target, raw)


def journal_mode_for_filesystem(fstype: Optional[str]) -> str:
    """Choose WAL only for a known local filesystem; unknown is fail-safe."""
    normalized = str(fstype or "").strip().lower()
    return "wal" if normalized in _LOCAL_WAL_FILESYSTEMS else "delete"


def journal_mode_for_path(path: Union[str, Path]) -> str:
    if str(path) == ":memory:":
        return "memory"
    return journal_mode_for_filesystem(filesystem_type_for_path(path))


def _establish_file_storage_mode(
        conn: sqlite3.Connection, *, required_mode: str) -> None:
    """Establish journal mode before any schema/data read.

    A previous release may have left a WAL on the shared path.  Entering
    EXCLUSIVE locking mode as the connection's first statement lets the sole
    lease-fenced process recover/switch that WAL without opening a wal-index
    shared-memory file on a takeover host.  Normal locking resumes immediately
    after the rollback journal is established.
    """
    try:
        if required_mode == "delete":
            locking = conn.execute("PRAGMA locking_mode = EXCLUSIVE").fetchone()[0].lower()
            if locking != "exclusive":
                raise SQLiteStorageModeError(
                    f"SQLite 无法进入共享盘迁移锁模式: {locking}")
            conn.execute("PRAGMA synchronous = FULL")
            synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
            actual_mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0].lower()
            normal = conn.execute("PRAGMA locking_mode = NORMAL").fetchone()[0].lower()
            if normal != "normal":
                raise SQLiteStorageModeError(
                    f"SQLite 无法恢复 normal locking mode: {normal}")
        else:
            actual_mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower()
            conn.execute("PRAGMA synchronous = FULL")
            synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    except SQLiteStorageModeError:
        raise
    except (sqlite3.Error, IndexError, AttributeError, TypeError) as error:
        raise SQLiteStorageModeError(
            f"无法在当前文件系统建立 SQLite {required_mode} journal") from error
    if actual_mode != required_mode:
        raise SQLiteStorageModeError(
            f"SQLite journal mode 未按文件系统收口: {actual_mode} != {required_mode}")
    if synchronous != 2:
        raise SQLiteStorageModeError(
            f"SQLite synchronous 未按存储合同收口: {synchronous} != 2")


def _read_migration() -> str:
    """Read and verify the frozen v1 migration (legacy public helper)."""
    return _read_locked_migration(MIGRATION_FILE, MIGRATION_SHA256)


def _read_locked_migration(path: Path, expected_hash: str) -> str:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise SchemaDriftError(f"migration 文件不可读：{path}") from error
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_hash:
        raise SchemaDriftError(
            f"migration 文件 checksum 漂移：{digest} ≠ 冻结锚 "
            f"{expected_hash}（{path}）")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SchemaDriftError(f"migration 文件不是 UTF-8：{path}") from error


def _read_migrations() -> Dict[int, str]:
    """Verify the complete known migration chain before opening a database."""
    return {
        1: _read_migration(),
        2: _read_locked_migration(
            BUNDLE_DAG_MIGRATION_FILE, BUNDLE_DAG_MIGRATION_SHA256),
    }


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


def _verify_schema_version(conn: sqlite3.Connection, version: int) -> None:
    expected = EXPECTED_COUNTS_BY_VERSION.get(version)
    if expected is None:
        raise SchemaDriftError(f"未知 schema 版本：user_version={version}")
    got = live_counts(conn)
    if got != expected:
        raise SchemaDriftError(f"schema 计数漂移：{got} ≠ {expected}")
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver != version:
        raise SchemaDriftError(
            f"schema 版本不符：user_version={ver} ≠ {version}")


def verify_schema(conn: sqlite3.Connection) -> None:
    _verify_schema_version(conn, SCHEMA_VERSION)


def _apply_migration(
        conn: sqlite3.Connection, *, version: int, migration_sql: str) -> None:
    """Apply one migration and its version bump in one SQLite transaction."""
    if version < 1 or version > SCHEMA_VERSION:
        raise SchemaDriftError(f"拒绝执行未知 migration 版本：{version}")
    script = (
        "BEGIN IMMEDIATE;\n"
        + migration_sql
        + f"\nPRAGMA user_version = {version};\n"
        + "COMMIT;\n"
    )
    try:
        conn.executescript(script)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def connect(path: Union[str, Path] = ":memory:") -> sqlite3.Connection:
    """Build, upgrade, or open a DB through the hash-locked migration chain.

    Every known migration checksum is verified on every connect.  Existing
    schemas are count/version checked *before* the next additive migration is
    allowed to run, preventing a drifted v1 database from being blessed as v2.
    """
    is_memory = str(path) == ":memory:"
    required_mode = "memory" if is_memory else journal_mode_for_path(path)
    migrations = _read_migrations()
    # The run process has one writer connection, but the interaction pump and
    # research driver use it from different threads.  WriteDaemon serializes
    # every access; disabling sqlite's thread-affinity check is therefore safe
    # and avoids opening a second writer connection.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        if not is_memory:
            _establish_file_storage_mode(conn, required_mode=required_mode)
        conn.execute("PRAGMA foreign_keys = ON")
        object_count = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if object_count == 0:
            if current != 0:
                raise SchemaDriftError(
                    f"空库却声明 schema 版本：user_version={current}")
        elif current == 0:
            raise SchemaDriftError("非空数据库缺 PRAGMA user_version")
        elif current > SCHEMA_VERSION:
            raise SchemaDriftError(
                f"schema 版本过新：user_version={current} > {SCHEMA_VERSION}")
        elif current not in EXPECTED_COUNTS_BY_VERSION:
            raise SchemaDriftError(f"未知 schema 版本：user_version={current}")

        while current < SCHEMA_VERSION:
            if current > 0:
                _verify_schema_version(conn, current)
            next_version = current + 1
            _apply_migration(
                conn,
                version=next_version,
                migration_sql=migrations[next_version],
            )
            current = next_version
        verify_schema(conn)
    except BaseException:
        try:
            conn.close()
        except BaseException:
            pass
        raise
    return conn
