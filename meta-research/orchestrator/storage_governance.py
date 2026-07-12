"""终态 cycle 的可重放存储快照主干。

SQLite 仍是唯一结构化真相。本模块只做 DB 提交后的幂等副作用：online backup、从同一备份
渲染并提交 runtime ``views/`` Git 仓、发布绑定 DB 已登记资产 path/hash 的内容寻址 manifest。
它不移动或删除已注册 checkpoint/log，也不引入后台服务或第二套数据库。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from . import database


SCHEMA = "meta-research-cycle-snapshot/v1"
GENESIS_SCHEMA = "meta-research-storage-genesis/v1"
TERMINAL_CYCLE_STATES = ("done", "aborted", "failed")
VIEW_FILES = ("goal.md", "tree.md", "pool.md", "digest.md")
_REF_NAME = re.compile(r"^c([1-9][0-9]*)\.json$")
_ATOMIC_TEMP = re.compile(
    r"^\.(?:c[1-9][0-9]*\.json|(?:goal|tree|pool|digest)\.md|[0-9a-f]{64}\.json)"
    r"\.tmp-[0-9a-f]{32}$")
_BACKUP_TEMP = re.compile(r"^c[1-9][0-9]*-[0-9a-f]{32}\.sqlite$")
_GENESIS_TEMP = re.compile(r"^\.genesis\.json\.tmp-[0-9a-f]{32}$")
_MAX_JSON_BYTES = 64 * 1024 * 1024


class StorageGovernanceError(RuntimeError):
    """备份、Git timeline 或不可变 receipt 不满足合同。"""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash_file(path: Path) -> Tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        return _hash_fd(fd, path)
    finally:
        os.close(fd)


def _hash_fd(fd: int, path: Path) -> Tuple[str, int]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise StorageGovernanceError(f"不是常规快照文件: {path}")
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def _ensure_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise StorageGovernanceError(f"治理目录类型非法: {path}")
    os.chmod(path, 0o700)


def _sync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard_unpublished_temps(path: Path, pattern: re.Pattern[str]) -> None:
    """删除严格命名、尚未原子提升的本组件临时文件；未知条目仍由后续扫描拒绝。"""
    changed = False
    for item in path.iterdir():
        if pattern.fullmatch(item.name) is None:
            continue
        info = item.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise StorageGovernanceError(f"临时对象类型非法: {item}")
        item.unlink()
        changed = True
    if changed:
        _sync_dir(path)


def _atomic_write(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(temporary, flags, mode)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    try:
        os.replace(temporary, path)
        os.chmod(path, mode)
        _sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path, *, maximum: int = _MAX_JSON_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise StorageGovernanceError(f"receipt 类型/大小非法: {path}")
        raw = bytearray()
        while len(raw) < info.st_size:
            block = os.read(fd, min(1024 * 1024, info.st_size - len(raw)))
            if not block:
                raise StorageGovernanceError(f"receipt 提前 EOF: {path}")
            raw.extend(block)
        return bytes(raw)
    finally:
        os.close(fd)


def _parse_json(raw: bytes, path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StorageGovernanceError(f"receipt JSON 损坏: {path}") from error
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise StorageGovernanceError(f"receipt 不是 canonical object: {path}")
    return value


def _read_json(path: Path) -> Dict[str, Any]:
    return _parse_json(_read(path), path)


def _publish_once(path: Path, raw: bytes) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        _atomic_write(path, raw, mode=0o400)
        return
    if _read(path, maximum=max(1, len(raw))) != raw:
        raise StorageGovernanceError(f"不可变对象内容漂移: {path}")


def _cell(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).replace("|", "\\|")


def _section(title: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = [f"## {title}", "", "| " + " | ".join(columns) + " |",
             "| " + " | ".join("---" for _ in columns) + " |"]
    count = 0
    for row in rows:
        lines.append("| " + " | ".join(_cell(item) for item in row) + " |")
        count += 1
    if count == 0:
        lines.append("| " + " | ".join("null" for _ in columns) + " |")
    return "\n".join(lines) + "\n"


class CycleSnapshotPublisher:
    """顺序补齐终态 cycle 的 backup → views commit → manifest pointer。"""

    def __init__(self, *, db_path: Path | str, work_root: Path | str,
                 owner_guard: Optional[Callable[[], None]] = None,
                 git_binary: Optional[str] = None):
        self.db_path = Path(os.path.abspath(os.fspath(db_path)))
        self.work_root = Path(os.path.abspath(os.fspath(work_root)))
        self.owner_guard = owner_guard or (lambda: None)
        candidate = git_binary or shutil.which("git", path=os.defpath)
        if candidate is None:
            raise StorageGovernanceError("views timeline 需要 git 可执行文件")
        try:
            trusted_git = Path(candidate).resolve(strict=True)
            info = trusted_git.stat()
        except OSError as error:
            raise StorageGovernanceError("git 可执行文件身份不可解析") from error
        if (not trusted_git.is_absolute() or not stat.S_ISREG(info.st_mode)
                or not os.access(trusted_git, os.X_OK)):
            raise StorageGovernanceError("git 须为可信系统路径下的常规可执行文件")
        self.git_binary = str(trusted_git)
        self.storage_root = self.work_root / "state" / "storage"
        self.genesis_path = self.storage_root / "genesis.json"
        self.backups = self.storage_root / "backups" / "sha256"
        self.manifests = self.storage_root / "manifests" / "sha256"
        self.cycles = self.storage_root / "cycles"
        self.pending = self.storage_root / "pending"
        self.temporary = self.storage_root / "tmp"
        self.views = self.work_root / "views"
        self._verified_high_water = 0
        self._verified_latest: Optional[Dict[str, Any]] = None
        self._coverage_start: Optional[int] = None
        self._genesis_identity: Optional[Tuple[int, bool, Optional[int]]] = None
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        self.owner_guard()
        if not self.work_root.is_dir() or self.work_root.is_symlink():
            raise StorageGovernanceError("work_root 须为已固定的非 symlink 目录")
        for path in (self.work_root / "state", self.storage_root,
                     self.storage_root / "backups", self.backups,
                     self.storage_root / "manifests", self.manifests,
                     self.cycles, self.pending, self.temporary, self.views):
            _ensure_dir(path)
        # kill 可能发生在 fsync(temp) 与 os.replace 之间。严格命名的 temp 从未成为完成事实，
        # 启动可安全丢弃；否则它会被 pointer/views 的未知文件检查永久卡住。
        for path in (self.cycles, self.pending, self.manifests, self.views):
            _discard_unpublished_temps(path, _ATOMIC_TEMP)
        _discard_unpublished_temps(self.temporary, _BACKUP_TEMP)
        _discard_unpublished_temps(self.storage_root, _GENESIS_TEMP)

    def reconcile(self, *, startup: bool = False) -> List[str]:
        """幂等补齐快照；返回本次新发布的 cycle id。

        首次 ``startup=True`` 会先冻结 genesis 覆盖边界：已有终态历史时只为最新终态建明确
        adoption 基线；尚无终态时从 c1 原生覆盖。这样首个 terminal 在 pending 前崩溃也不会在重启后
        被误判成旧库接管。runtime 首次调用默认按原生覆盖处理。
        """
        self.owner_guard()
        terminal = self._terminal_cycles()
        refs = self._refs()
        terminal_ids = {item[0] for item in terminal}
        terminal_status = dict(terminal)
        if set(refs) - terminal_ids:
            raise StorageGovernanceError("已有 snapshot pointer 指向非终态或不存在的 cycle")
        pending_ids = self._pending_ids()
        if pending_ids - terminal_ids:
            raise StorageGovernanceError("pending snapshot 指向非终态或不存在的 cycle")
        genesis = self._genesis(
            terminal, refs=refs, pending_ids=pending_ids, startup=startup)
        if not terminal:
            return []
        if genesis["adoption_baseline"] and not refs:
            cycle_id = genesis["coverage_start_cycle"]
            if terminal[-1][0] != cycle_id:
                raise StorageGovernanceError(
                    "adoption genesis 后出现未发布终态；拒绝用当前 DB 伪造旧 recovery point")
            status = terminal_status[cycle_id]
            self._publish(
                cycle_id, status,
                bootstrap_before=genesis["bootstrap_before_cycle"],
                adoption_baseline=True, previous=None)
            return [f"c{cycle_id}"]
        if not refs and terminal[0][0] != genesis["coverage_start_cycle"]:
            raise StorageGovernanceError(
                "首个未发布终态 cycle 与原生 genesis coverage 起点不一致")
        published: List[str] = []
        last = max(refs, default=0)
        previous = None
        if last:
            previous = self._validate_chain(terminal, refs, genesis)
            if last in pending_ids:
                pending = _read_json(self.pending / f"c{last}.json")
                backup = self._validate_pending(last, terminal_status[last], pending)
                if backup != previous.get("backup"):
                    raise StorageGovernanceError("已完成 pointer 与遗留 pending backup 不一致")
                (self.pending / f"c{last}.json").unlink()
                _sync_dir(self.pending)
            if any(item <= last and item not in refs for item in pending_ids):
                raise StorageGovernanceError("旧 pending snapshot 位于已发布 high-water 之前")
        missing = [(cycle_id, status) for cycle_id, status in terminal if cycle_id > last]
        # 正常运行在每个 terminal commit 后同步发布，故最多缺一轮。多轮缺口已经失去精确切面，
        # 不能拿同一个当前 DB 冒充多个历史 recovery point。
        if len(missing) > 1:
            raise StorageGovernanceError("发现多个未发布终态 cycle；拒绝伪造历史逐轮 snapshot")
        if previous is not None:
            self._validate_runtime_git(
                previous,
                allow_pending_next=bool(missing and missing[0][0] in pending_ids))
        for cycle_id, status in missing:
            self._publish(
                cycle_id, status, bootstrap_before=None,
                adoption_baseline=False, previous=previous)
            published.append(f"c{cycle_id}")
        return published

    def _genesis(self, terminal: Sequence[Tuple[int, str]], *, refs: Mapping[int, Path],
                 pending_ids: set[int], startup: bool) -> Dict[str, Any]:
        """冻结一次性的 coverage 起点；只描述首 pointer，不另建运行状态机。"""
        try:
            marker = _read_json(self.genesis_path)
        except FileNotFoundError:
            if refs or pending_ids:
                raise StorageGovernanceError(
                    "snapshot 已有 pointer/pending 但 genesis 缺失；拒绝从当前 DB 猜测历史边界")
            if startup and terminal:
                start = terminal[-1][0]
                marker = {
                    "schema": GENESIS_SCHEMA,
                    "coverage_start_cycle": start,
                    "adoption_baseline": True,
                    "bootstrap_before_cycle": start - 1,
                }
            else:
                marker = {
                    "schema": GENESIS_SCHEMA,
                    "coverage_start_cycle": 1,
                    "adoption_baseline": False,
                    "bootstrap_before_cycle": None,
                }
            self.owner_guard()
            _publish_once(self.genesis_path, _canonical(marker))
            self.owner_guard()
            marker = _read_json(self.genesis_path)
        except OSError as error:
            raise StorageGovernanceError("storage genesis 缺失/不可读") from error

        start = marker.get("coverage_start_cycle")
        adoption = marker.get("adoption_baseline")
        bootstrap = marker.get("bootstrap_before_cycle")
        if (marker.get("schema") != GENESIS_SCHEMA
                or not isinstance(start, int) or isinstance(start, bool) or start < 1
                or not isinstance(adoption, bool)
                or (adoption and bootstrap != start - 1)
                or (not adoption and (start != 1 or bootstrap is not None))):
            raise StorageGovernanceError("storage genesis 漂移")
        identity = (start, adoption, bootstrap)
        if self._genesis_identity is not None and self._genesis_identity != identity:
            raise StorageGovernanceError("已验证 storage genesis 被替换")
        self._genesis_identity = identity
        return marker

    def _terminal_cycles(self) -> List[Tuple[int, str]]:
        try:
            conn = sqlite3.connect(
                f"file:{quote(str(self.db_path))}?mode=ro", uri=True)
        except sqlite3.Error as error:
            raise StorageGovernanceError("无法只读打开 SQLite 真相") from error
        try:
            marks = ",".join("?" for _ in TERMINAL_CYCLE_STATES)
            rows = conn.execute(
                f"SELECT id,status FROM cycle WHERE status IN ({marks}) ORDER BY id",
                TERMINAL_CYCLE_STATES).fetchall()
            return [(int(row[0]), str(row[1])) for row in rows]
        finally:
            conn.close()

    def _refs(self) -> Dict[int, Path]:
        refs: Dict[int, Path] = {}
        for path in self.cycles.iterdir():
            match = _REF_NAME.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise StorageGovernanceError(f"snapshot pointer 目录含非法条目: {path.name}")
            refs[int(match.group(1))] = path
        return refs

    def _pending_ids(self) -> set[int]:
        result: set[int] = set()
        for path in self.pending.iterdir():
            match = _REF_NAME.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise StorageGovernanceError(f"pending snapshot 目录含非法条目: {path.name}")
            result.add(int(match.group(1)))
        return result

    def _publish(self, cycle_id: int, status: str, *, bootstrap_before: Optional[int],
                 adoption_baseline: bool, previous: Optional[Mapping[str, Any]]) -> None:
        self.owner_guard()
        pointer_path = self.cycles / f"c{cycle_id}.json"
        if pointer_path.exists():
            self._validate_pointer(cycle_id)
            return
        pending_path = self.pending / f"c{cycle_id}.json"
        if pending_path.exists():
            pending = _read_json(pending_path)
            backup = self._validate_pending(cycle_id, status, pending)
            bootstrap_before = pending.get("bootstrap_before_cycle")
            adoption_baseline = pending.get("adoption_baseline") is True
        else:
            backup = self._backup(
                cycle_id, status, allow_later_cycles=adoption_baseline)
            pending = {
                "schema": SCHEMA, "cycle_id": f"c{cycle_id}", "cycle_status": status,
                "bootstrap_before_cycle": bootstrap_before,
                "adoption_baseline": adoption_baseline, "backup": backup,
            }
            _atomic_write(pending_path, _canonical(pending))

        self.owner_guard()
        backup_path = self.work_root / backup["path"]
        assets = self._assets(backup_path)
        asset_hash = _hash_bytes(_canonical(assets))
        expected_parent = None if previous is None else previous["views"]["commit"]
        views = self._publish_views(
            cycle_id, status, backup_path, expected_parent=expected_parent,
            backup_hash=backup["sha256"], asset_hash=asset_hash)
        manifest = {
            "schema": SCHEMA,
            "cycle_id": f"c{cycle_id}",
            "cycle_status": status,
            "bootstrap_before_cycle": bootstrap_before,
            "adoption_baseline": adoption_baseline,
            "previous_manifest_sha256": (
                None if previous is None else previous["manifest_sha256"]),
            "backup": backup,
            "views": views,
            "asset_inventory_sha256": asset_hash,
            "assets": assets,
        }
        manifest_raw = _canonical(manifest)
        manifest_hash = _hash_bytes(manifest_raw)
        manifest_rel = Path("state/storage/manifests/sha256") / f"{manifest_hash}.json"
        self.owner_guard()
        _publish_once(self.work_root / manifest_rel, manifest_raw)
        pointer = {
            "schema": SCHEMA,
            "cycle_id": f"c{cycle_id}",
            "manifest_sha256": manifest_hash,
            "manifest_path": manifest_rel.as_posix(),
        }
        self.owner_guard()             # pointer 是完成闸；失去 owner 后绝不替新 owner 发布
        _publish_once(pointer_path, _canonical(pointer))
        self.owner_guard()
        published = dict(manifest)
        published["manifest_sha256"] = manifest_hash
        self._remember_verified(cycle_id, published)
        pending_path.unlink(missing_ok=True)
        _sync_dir(self.pending)

    def _backup(self, cycle_id: int, status: str, *,
                allow_later_cycles: bool = False) -> Dict[str, Any]:
        temporary = self.temporary / f"c{cycle_id}-{uuid.uuid4().hex}.sqlite"
        source: Optional[sqlite3.Connection] = None
        target: Optional[sqlite3.Connection] = None
        try:
            self.owner_guard()
            source = sqlite3.connect(
                f"file:{quote(str(self.db_path))}?mode=ro", uri=True,
                check_same_thread=False)
            target = sqlite3.connect(str(temporary))
            source.backup(target)
            # Recovery object must be one immutable file.  Inheriting WAL mode would let later
            # verification reads create mutable ``-wal/-shm`` siblings beside the CAS object.
            if target.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise StorageGovernanceError("SQLite backup 无法收敛为单文件 recovery object")
            if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise StorageGovernanceError("SQLite backup quick_check 失败")
            if target.execute("PRAGMA foreign_key_check").fetchall():
                raise StorageGovernanceError("SQLite backup foreign_key_check 失败")
            database.verify_schema(target)
            if target.execute("SELECT status FROM cycle WHERE id=?", (cycle_id,)).fetchone() != (status,):
                raise StorageGovernanceError("SQLite backup 未包含预期终态 cycle")
            if (not allow_later_cycles
                    and target.execute("SELECT MAX(id) FROM cycle").fetchone() != (cycle_id,)):
                raise StorageGovernanceError("SQLite backup 不是预期 cycle 的精确终态切面")
            target.close()
            target = None
            source.close()
            source = None
            fd = os.open(temporary, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            digest, size = _hash_file(temporary)
            destination = self.backups / f"{digest}.sqlite"
            self.owner_guard()
            if destination.exists():
                if _hash_file(destination) != (digest, size):
                    raise StorageGovernanceError("backup CAS 对象漂移")
                temporary.unlink()
            else:
                os.chmod(temporary, 0o400)
                os.replace(temporary, destination)
                _sync_dir(self.backups)
            return {
                "sha256": digest,
                "bytes": size,
                "path": destination.relative_to(self.work_root).as_posix(),
                "schema_version": database.SCHEMA_VERSION,
            }
        except (sqlite3.Error, OSError) as error:
            raise StorageGovernanceError(f"cycle c{cycle_id} online backup 失败") from error
        finally:
            if target is not None:
                target.close()
            if source is not None:
                source.close()
            temporary.unlink(missing_ok=True)

    def _validate_pending(self, cycle_id: int, status: str,
                          pending: Mapping[str, Any]) -> Dict[str, Any]:
        adoption = pending.get("adoption_baseline")
        bootstrap_before = pending.get("bootstrap_before_cycle")
        if (pending.get("schema") != SCHEMA or pending.get("cycle_id") != f"c{cycle_id}"
                or pending.get("cycle_status") != status
                or not isinstance(adoption, bool)
                or (adoption and bootstrap_before != cycle_id - 1)
                or (not adoption and bootstrap_before is not None)
                or not isinstance(pending.get("backup"), dict)):
            raise StorageGovernanceError(f"cycle c{cycle_id} pending receipt 漂移")
        backup = dict(pending["backup"])
        digest = backup.get("sha256")
        size = backup.get("bytes")
        if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or not isinstance(size, int) or size <= 0
                or backup.get("schema_version") != database.SCHEMA_VERSION):
            raise StorageGovernanceError("pending backup identity 漂移")
        expected = self.backups / f"{digest}.sqlite"
        if backup.get("path") != expected.relative_to(self.work_root).as_posix():
            raise StorageGovernanceError("pending backup 路径漂移")
        self._verify_backup_object(
            expected, expected_hash=digest, expected_bytes=size,
            cycle_id=cycle_id, cycle_status=status,
            allow_later_cycles=adoption)
        return backup

    def _verify_backup_object(self, path: Path, *, expected_hash: str, expected_bytes: int,
                              cycle_id: int, cycle_status: str,
                              allow_later_cycles: bool) -> None:
        """用同一 O_NOFOLLOW fd 完成 hash 与 SQLite 语义核验，拒绝 hash→reopen 替换。"""
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as error:
            raise StorageGovernanceError(f"cycle c{cycle_id} backup 缺失/不可读") from error
        conn: Optional[sqlite3.Connection] = None
        try:
            if _hash_fd(fd, path) != (expected_hash, expected_bytes):
                raise StorageGovernanceError(f"cycle c{cycle_id} backup 内容漂移")
            # `/proc/self/fd/N` anchors the inode held above; immutable=1 prevents journal sidecars.
            capability = f"/proc/self/fd/{fd}"
            conn = sqlite3.connect(
                f"file:{quote(capability)}?mode=ro&immutable=1", uri=True)
            if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise StorageGovernanceError(f"cycle c{cycle_id} backup quick_check 失败")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise StorageGovernanceError(f"cycle c{cycle_id} backup foreign_key_check 失败")
            database.verify_schema(conn)
            if conn.execute("SELECT status FROM cycle WHERE id=?", (cycle_id,)).fetchone() != (
                    cycle_status,):
                raise StorageGovernanceError(
                    f"cycle c{cycle_id} backup 未包含预期终态 {cycle_status}")
            if (not allow_later_cycles
                    and conn.execute("SELECT MAX(id) FROM cycle").fetchone() != (cycle_id,)):
                raise StorageGovernanceError(
                    f"cycle c{cycle_id} backup 不是该轮精确终态切面")
        except sqlite3.Error as error:
            raise StorageGovernanceError(f"cycle c{cycle_id} backup SQLite 核验失败") from error
        finally:
            if conn is not None:
                conn.close()
            os.close(fd)

    def _assets(self, backup_path: Path) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(f"file:{quote(str(backup_path))}?mode=ro", uri=True)
        try:
            assets: List[Dict[str, Any]] = []
            for row in conn.execute(
                    "SELECT id,path,hash_alg,content_hash,artifact_type,origin,manifest_hash "
                    "FROM checkpoint ORDER BY id"):
                assets.append({
                    "owner": "checkpoint", "owner_id": int(row[0]), "ref": row[1],
                    "hash_alg": row[2], "content_hash": row[3],
                    "artifact_type": row[4], "origin": row[5], "manifest_hash": row[6],
                    "retention": "registered_forever",
                })
            for row in conn.execute(
                    "SELECT id,ref,content_hash,bytes,log_kind,cycle_id FROM execution_log ORDER BY id"):
                assets.append({
                    "owner": "execution_log", "owner_id": int(row[0]), "ref": row[1],
                    "hash_alg": "sha256", "content_hash": row[2], "bytes": row[3],
                    "log_kind": row[4], "cycle_id": f"c{row[5]}",
                    "retention": "registered_forever",
                })
            for row in conn.execute(
                    "SELECT id,manifest_hash,action_cycle FROM external_import "
                    "WHERE action='imported' ORDER BY id"):
                assets.append({
                    "owner": "external_import", "owner_id": int(row[0]),
                    "provenance_manifest_hash": row[1], "cycle_id": f"c{row[2]}",
                    "retention": "db_provenance_only",
                })
            return assets
        finally:
            conn.close()

    def _publish_views(self, cycle_id: int, status: str, backup_path: Path, *,
                       expected_parent: Optional[str], backup_hash: str,
                       asset_hash: str) -> Dict[str, str]:
        self.owner_guard()
        self._ensure_git_repo()
        unknown = set(self._git_z("ls-files", "--others", "--exclude-standard")) - set(VIEW_FILES)
        tracked = set(self._git_z("ls-files"))
        if unknown or tracked - set(VIEW_FILES):
            raise StorageGovernanceError("views Git 含治理范围外文件")
        for name, text in self._render_views(cycle_id, status, backup_path).items():
            _atomic_write(self.views / name, text.encode("utf-8"))
        self._git("add", "--", *VIEW_FILES)
        desired_tree = self._git("write-tree").stdout.strip()
        head = self._head()
        subject = f"cycle c{cycle_id} storage snapshot"
        body = (f"Cycle: c{cycle_id}\nDB-Backup-SHA256: {backup_hash}\n"
                f"Asset-Inventory-SHA256: {asset_hash}")
        if head is not None and self._git("rev-parse", "HEAD^{tree}").stdout.strip() == desired_tree:
            self._validate_commit(head, expected_parent, subject, body)
        else:
            if head != expected_parent:
                raise StorageGovernanceError("views Git HEAD 与上一 snapshot 不连续")
            self._git("commit", "--quiet", "--allow-empty", "--no-verify",
                      "-m", subject, "-m", body)
            head = self._head()
            self._validate_commit(head, expected_parent, subject, body)
        self.owner_guard()             # owner loss after commit leaves resumable orphan, never pointer
        if self._git("status", "--porcelain", "--untracked-files=all").stdout:
            raise StorageGovernanceError("views Git 提交后仍有工作区漂移")
        return {"path": "views", "commit": str(head), "tree": desired_tree}

    def _ensure_git_repo(self) -> None:
        git_dir = self.views / ".git"
        if git_dir.exists():
            info = git_dir.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise StorageGovernanceError("views/.git 类型非法")
        else:
            entries = {item.name for item in self.views.iterdir()}
            if entries - set(VIEW_FILES):
                raise StorageGovernanceError("初始化 views Git 前存在未知文件")
            self._git("init", "--quiet")
            self._git("config", "--local", "user.name", "meta-research")
            self._git("config", "--local", "user.email", "meta-research@localhost")
        top = Path(self._git("rev-parse", "--show-toplevel").stdout.strip())
        if top != self.views:
            raise StorageGovernanceError("views Git 顶层目录漂移")

    def _require_git_repo(self) -> None:
        """只读验证既有 repo；验证路径绝不把丢失的 `.git` 修成一个空仓。"""
        git_dir = self.views / ".git"
        try:
            info = git_dir.lstat()
        except FileNotFoundError as error:
            raise StorageGovernanceError("views Git 仓缺失") from error
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise StorageGovernanceError("views/.git 类型非法")
        top = Path(self._git("rev-parse", "--show-toplevel").stdout.strip())
        if top != self.views:
            raise StorageGovernanceError("views Git 顶层目录漂移")

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "meta-research", "GIT_AUTHOR_EMAIL": "meta-research@localhost",
            "GIT_COMMITTER_NAME": "meta-research", "GIT_COMMITTER_EMAIL": "meta-research@localhost",
        }
        command = [self.git_binary, "-c", "core.hooksPath=/dev/null",
                   "-c", "commit.gpgSign=false", "-C", str(self.views), *args]
        result = subprocess.run(command, text=True, capture_output=True, timeout=30, env=env)
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise StorageGovernanceError(f"views Git {args[0]} 失败: {detail}")
        return result

    def _git_z(self, *args: str) -> List[str]:
        return [item for item in self._git(*args, "-z").stdout.split("\0") if item]

    def _head(self) -> Optional[str]:
        result = self._git("rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def _validate_commit(self, head: Optional[str], expected_parent: Optional[str],
                         subject: str, body: str) -> None:
        if head is None:
            raise StorageGovernanceError("views Git 未生成 commit")
        message = self._git("show", "-s", "--format=%B", head).stdout.strip()
        parents = self._git("rev-list", "--parents", "-n", "1", head).stdout.strip().split()[1:]
        expected_parents = [] if expected_parent is None else [expected_parent]
        if message != f"{subject}\n\n{body}" or parents != expected_parents:
            raise StorageGovernanceError("views Git commit 身份或父链漂移")

    def _validate_runtime_git(self, latest: Mapping[str, Any], *,
                              allow_pending_next: bool) -> None:
        """无合法 next-pending 时，HEAD/worktree 必须精确停在最新完成 pointer。"""
        if allow_pending_next:
            return                    # `_publish_views` 会核 orphan 的 tree/message/parent 后复用或拒绝
        self._require_git_repo()
        if self._head() != latest["views"]["commit"]:
            raise StorageGovernanceError("views Git HEAD 超出最新完成 snapshot")
        if self._git("status", "--porcelain", "--untracked-files=all").stdout:
            raise StorageGovernanceError("views Git worktree 与最新完成 snapshot 不一致")

    def _remember_verified(self, cycle_id: int, manifest: Mapping[str, Any]) -> None:
        self._verified_high_water = cycle_id
        self._verified_latest = dict(manifest)
        if self._coverage_start is None:
            self._coverage_start = cycle_id

    def _render_views(self, cycle_id: int, status: str,
                      backup_path: Path) -> Dict[str, str]:
        conn = sqlite3.connect(f"file:{quote(str(backup_path))}?mode=ro", uri=True)
        try:
            prefix = ("<!-- 由不可变 SQLite 备份机械生成；请勿编辑 -->\n"
                      f"# 轮次 c{cycle_id} 存储投影\n\n- 轮次状态：`{status}`\n\n")
            goal = prefix + _section(
                "目标", ("编号", "版本", "正文", "完成谓词"),
                conn.execute("SELECT id,version,text,predicate_json FROM goal ORDER BY id,version"))
            tree = prefix + _section(
                "问题树", ("编号", "父问题", "目标", "目标版本", "状态", "访问次数", "分数", "正文"),
                conn.execute("SELECT id,parent_id,goal_id,goal_ver,status,visit_count,score,text "
                             "FROM question ORDER BY id"))
            pool = [prefix]
            for title, columns, sql in (
                ("基线", ("编号", "短名", "规范键", "状态"),
                 "SELECT id,slug,canonical_key,status FROM baseline ORDER BY id"),
                ("变体", ("编号", "基线", "变体键", "状态", "环境哈希"),
                 "SELECT id,baseline_id,variant_key,status,env_hash FROM variant ORDER BY id"),
                ("检查点", ("编号", "变体", "检查点键", "路径", "内容哈希", "算法", "来源"),
                 "SELECT id,variant_id,ckpt_key,path,content_hash,hash_alg,origin FROM checkpoint ORDER BY id"),
                ("评测", ("编号", "变体", "协议", "协议版本", "评测键", "状态", "规范尝试"),
                 "SELECT id,variant_id,protocol_id,protocol_ver,eval_key,status,canonical_attempt_id "
                 "FROM evaluation ORDER BY id"),
            ):
                pool.extend((_section(title, columns, conn.execute(sql)), "\n"))
            digest = [prefix, _section(
                "轮次", ("编号", "目标", "目标版本", "当前问题", "状态", "路径", "下一问题", "下一意图", "失败类型"),
                conn.execute("SELECT id,goal_id,goal_ver,active_question_id,status,route,"
                             "next_question_id,next_intent,failure_kind FROM cycle WHERE id=?", (cycle_id,))), "\n",
                _section("决策", ("编号", "参与者", "类型", "载荷"),
                         conn.execute("SELECT id,actor,type,payload_json FROM decision "
                                      "WHERE cycle_id=? ORDER BY id", (cycle_id,))), "\n",
                _section("模型调用", ("编号", "阶段", "目的", "状态", "记录引用", "失败类型"),
                         conn.execute("SELECT id,phase,purpose,status,transcript_ref,failure_kind "
                                      "FROM runner_call WHERE cycle_id=? ORDER BY id", (cycle_id,))), "\n"]
            return {"goal.md": goal, "tree.md": tree,
                    "pool.md": "".join(pool), "digest.md": "".join(digest)}
        finally:
            conn.close()

    def _validate_chain(self, terminal: Sequence[Tuple[int, str]],
                        refs: Mapping[int, Path],
                        genesis: Mapping[str, Any]) -> Dict[str, Any]:
        ordered = sorted(refs)
        last = ordered[-1]
        marker_start = genesis["coverage_start_cycle"]
        if ordered[0] != marker_start:
            raise StorageGovernanceError("snapshot chain 首轮与 genesis coverage 起点不一致")
        if self._verified_latest is not None:
            coverage_start = self._coverage_start
            assert coverage_start is not None
            if coverage_start != marker_start:
                raise StorageGovernanceError("已验证 snapshot coverage 与 genesis 漂移")
        else:
            first_manifest = self._validate_pointer(
                ordered[0], verify_backup=ordered[0] == last, verify_git=False)
            if (first_manifest.get("adoption_baseline")
                    is not genesis["adoption_baseline"]
                    or first_manifest.get("bootstrap_before_cycle")
                    != genesis["bootstrap_before_cycle"]):
                raise StorageGovernanceError("snapshot chain 首 pointer 与 genesis 声明不一致")
            coverage_start = marker_start
        expected = {cycle_id for cycle_id, _status in terminal
                    if coverage_start <= cycle_id <= last}
        if set(refs) != expected:
            raise StorageGovernanceError("snapshot high-water 内存在 pointer 缺口")

        # 驻留进程只验证新增 high-water；历史 coverage 仍靠 pointer 名集合机械核缺口。
        if self._verified_latest is not None:
            latest = self._validate_pointer(last, verify_backup=True, verify_git=True)
            if last == self._verified_high_water:
                if latest["manifest_sha256"] != self._verified_latest["manifest_sha256"]:
                    raise StorageGovernanceError("已验证 high-water manifest 被替换")
                return latest
            if (last <= self._verified_high_water
                    or latest.get("previous_manifest_sha256")
                    != self._verified_latest["manifest_sha256"]
                    or latest.get("adoption_baseline") is not False
                    or latest.get("bootstrap_before_cycle") is not None):
                raise StorageGovernanceError("新增 snapshot 未接到已验证 high-water")
            parents = self._git(
                "rev-list", "--parents", "-n", "1", latest["views"]["commit"]
            ).stdout.strip().split()[1:]
            if parents != [self._verified_latest["views"]["commit"]]:
                raise StorageGovernanceError("新增 views commit 未接到已验证 high-water")
            self._remember_verified(last, latest)
            return latest

        manifests: List[Dict[str, Any]] = []
        previous_hash = None
        for position, cycle_id in enumerate(ordered):
            manifest = (first_manifest if position == 0 else self._validate_pointer(
                cycle_id, verify_backup=cycle_id == last, verify_git=False))
            if manifest.get("previous_manifest_sha256") != previous_hash:
                raise StorageGovernanceError(f"cycle c{cycle_id} manifest parent 漂移")
            if position > 0 and (manifest.get("adoption_baseline") is not False
                                 or manifest.get("bootstrap_before_cycle") is not None):
                raise StorageGovernanceError("adoption 标记只能出现在 chain 首个 pointer")
            previous_hash = manifest["manifest_sha256"]
            manifests.append(manifest)

        # 一次 Git 调用核完整 first-parent commit/tree 序列，避免每轮对全部历史起 O(N) 子进程。
        self._require_git_repo()
        latest_commit = manifests[-1]["views"]["commit"]
        result = self._git(
            "log", "--first-parent", "--reverse", "--format=%H %T", latest_commit)
        git_chain = [tuple(line.split()) for line in result.stdout.splitlines() if line.strip()]
        expected_git = [(item["views"]["commit"], item["views"]["tree"])
                        for item in manifests]
        if git_chain != expected_git:
            raise StorageGovernanceError("views Git commit/tree chain 与 snapshot pointers 不一致")
        latest = manifests[-1]
        self._coverage_start = coverage_start
        self._remember_verified(last, latest)
        return latest

    def _validate_pointer(self, cycle_id: int, *, verify_backup: bool = True,
                          verify_git: bool = True) -> Dict[str, Any]:
        try:
            pointer = _read_json(self.cycles / f"c{cycle_id}.json")
        except OSError as error:
            raise StorageGovernanceError(f"cycle c{cycle_id} pointer 缺失/不可读") from error
        digest = pointer.get("manifest_sha256")
        expected = Path("state/storage/manifests/sha256") / f"{digest}.json"
        if (pointer.get("schema") != SCHEMA or pointer.get("cycle_id") != f"c{cycle_id}"
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or pointer.get("manifest_path") != expected.as_posix()):
            raise StorageGovernanceError(f"cycle c{cycle_id} pointer 漂移")
        manifest_path = self.work_root / expected
        try:
            raw = _read(manifest_path)
        except OSError as error:
            raise StorageGovernanceError(f"cycle c{cycle_id} manifest 缺失/不可读") from error
        if _hash_bytes(raw) != digest:
            raise StorageGovernanceError(f"cycle c{cycle_id} manifest hash 漂移")
        # Parse exactly the bytes whose digest was checked above.  Reopening by path here would
        # admit same-uid replace between hash(A) and parse(B).
        manifest = _parse_json(raw, manifest_path)
        if manifest.get("schema") != SCHEMA or manifest.get("cycle_id") != f"c{cycle_id}":
            raise StorageGovernanceError(f"cycle c{cycle_id} manifest identity 漂移")
        backup = manifest.get("backup")
        if not isinstance(backup, dict):
            raise StorageGovernanceError(f"cycle c{cycle_id} manifest 缺 backup")
        backup_hash = backup.get("sha256")
        backup_path = self.backups / f"{backup_hash}.sqlite"
        backup_shape_ok = (
            isinstance(backup_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", backup_hash) is not None
            and backup.get("path") == backup_path.relative_to(self.work_root).as_posix()
            and backup.get("schema_version") == database.SCHEMA_VERSION
            and isinstance(backup.get("bytes"), int) and backup["bytes"] > 0)
        if not backup_shape_ok:
            raise StorageGovernanceError(f"cycle c{cycle_id} backup identity 漂移")
        if verify_backup:
            self._verify_backup_object(
                backup_path, expected_hash=backup_hash, expected_bytes=backup["bytes"],
                cycle_id=cycle_id, cycle_status=manifest.get("cycle_status"),
                allow_later_cycles=manifest.get("adoption_baseline") is True)
        assets = manifest.get("assets")
        if (not isinstance(assets, list)
                or _hash_bytes(_canonical(assets)) != manifest.get("asset_inventory_sha256")):
            raise StorageGovernanceError(f"cycle c{cycle_id} asset inventory 漂移")
        views = manifest.get("views")
        if not isinstance(views, dict) or views.get("path") != "views":
            raise StorageGovernanceError(f"cycle c{cycle_id} views identity 漂移")
        commit = views.get("commit")
        tree = views.get("tree")
        git_shape_ok = (
            isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40,64}", commit) is not None
            and isinstance(tree, str) and re.fullmatch(r"[0-9a-f]{40,64}", tree) is not None)
        if not git_shape_ok:
            raise StorageGovernanceError(f"cycle c{cycle_id} views commit/tree 漂移")
        if verify_git:
            self._require_git_repo()
            if (self._git("cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode != 0
                    or self._git("rev-parse", f"{commit}^{{tree}}").stdout.strip() != tree):
                raise StorageGovernanceError(f"cycle c{cycle_id} views commit/tree 漂移")
        result = dict(manifest)
        result["manifest_sha256"] = digest
        return result
