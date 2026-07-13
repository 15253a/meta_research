"""Registered non-DB asset operations layered on the cycle snapshot spine.

Only assets with an immutable SQLite identity belong here.  The first slice mirrors rows from
``execution_log``; it deliberately does not glob runner transcripts, guardian captures, or sandbox
session state, whose authorities and recovery lifecycles are different.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import sqlite3
import stat
import uuid
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import quote

from . import storage_governance as sg
from .instance_lease import RESTORE_IN_PROGRESS_NAME, InstanceLease
from .storage_paths import (
    RegisteredPathError,
    registered_path_roots,
    resolve_registered_path,
    validate_restore_target_lineage,
)
from .storage_restore_contract import (
    REGISTERED_COMPLETION_RELATIVE,
    REGISTERED_RESTORE_MARKER,
    REGISTERED_RESTORE_SCHEMA,
    StorageRestoreContractError,
    read_marker,
    validate_registered_completion,
)


LOG_MIRROR_SCHEMA = "meta-research-execution-log-mirror/v1"
LOG_MIRROR_REPORT_SCHEMA = "meta-research-log-mirror-report/v1"
GZIP_PROFILE = "gzip-deflate9-mtime0-empty-name-os255/v1"
_HASH_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
_BARE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_RE = re.compile(r"^([0-9a-f]{64})\.gz$")
_INDEX_RE = re.compile(r"^execution-log-([1-9][0-9]*)\.json$")
_TEMP_RE = re.compile(r"^\.[0-9a-f]{32}\.gz\.tmp$")
_INDEX_TEMP_RE = re.compile(
    r"^\.execution-log-[1-9][0-9]*\.json\.tmp-[0-9a-f]{32}$")
_CAPACITY_MARGIN = 1 * 1024 * 1024
_GZIP_HEADER = bytes.fromhex("1f8b08000000000002ff")
CHECKPOINT_MIRROR_SCHEMA = "meta-research-checkpoint-mirror/v1"
CHECKPOINT_MIRROR_REPORT_SCHEMA = "meta-research-checkpoint-mirror-report/v1"
REGISTERED_RESTORE_REPORT_SCHEMA = "meta-research-registered-asset-restore-report/v1"
REGISTERED_SET_REPORT_SCHEMA = "meta-research-registered-asset-set/v1"
_CHECKPOINT_OBJECT_RE = re.compile(r"^[0-9a-f]{64}$")
_CHECKPOINT_INDEX_RE = re.compile(r"^checkpoint-([1-9][0-9]*)\.json$")
_CHECKPOINT_TEMP_RE = re.compile(r"^\.[0-9a-f]{32}\.tmp$")
_CHECKPOINT_INDEX_TEMP_RE = re.compile(
    r"^\.checkpoint-[1-9][0-9]*\.json\.tmp-[0-9a-f]{32}$")
_RESTORE_TEMP_SUFFIX_RE = re.compile(r"^[0-9a-f]{32}$")
_RESTORE_CAPACITY_MARGIN_BYTES = 1 * 1024 * 1024
_RESTORE_CAPACITY_MARGIN_INODES = 32
_REGISTERED_RECEIPT_MAX_BYTES = 64 * 1024 * 1024
_RESERVED_REGISTERED_TOP_LEVEL = {
    "research.sqlite", "restore.json", RESTORE_IN_PROGRESS_NAME,
    ".orchestrator-instance.lock", "state", "views",
}


class StorageAssetError(sg.StorageGovernanceError):
    """Registered asset or its immutable mirror is incomplete/corrupt."""


def _normal_hash(value: Any, *, label: str) -> str:
    match = _HASH_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise StorageAssetError(f"{label} 不是规范 SHA256")
    return match.group(1)


def _registered_relative(value: str, *, label: str) -> Path:
    relative = Path(value)
    if (not value or relative.is_absolute()
            or relative.as_posix() != value
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.parts[0] in _RESERVED_REGISTERED_TOP_LEVEL
            or relative.parts[0].startswith("research.sqlite-")):
        if relative.parts and relative.parts[0] == "state":
            raise StorageAssetError(
                f"{label} state/ namespace 是可写控制面保留路径")
        raise StorageAssetError(f"{label} hydration path 非法/保留")
    return relative


class RegisteredAssetArchive:
    """Offline registered-asset primitive; caller supplies a lease-fenced SnapshotArchive."""

    def __init__(self, snapshot_archive):  # noqa: ANN001 - avoid a storage_ops import cycle
        self.snapshot_archive = snapshot_archive
        self.work_root = snapshot_archive.work_root
        self.owner_guard = snapshot_archive.owner_guard
        self.root = snapshot_archive.publisher.storage_root / "log-mirrors"
        self.objects = self.root / "objects" / "sha256"
        self.indexes = self.root / "indexes"
        self.checkpoint_root = (
            snapshot_archive.publisher.storage_root / "checkpoint-mirrors")
        self.checkpoint_objects = (
            self.checkpoint_root / "objects" / "sha256")
        self.checkpoint_indexes = self.checkpoint_root / "indexes"

    def _latest(self) -> tuple[Dict[str, Any], Path, Dict[str, Any]]:
        chain = self.snapshot_archive._chain(retain=3)
        cycle_id = chain["ordered"][-1]
        manifest = chain["manifests"][-1]
        backup = self.work_root / manifest["backup"]["path"]
        if f"c{cycle_id}" not in chain["deep_verified"]:
            raise StorageAssetError("latest snapshot 未完成 retained SQLite 深验")
        return {
            "high_water_cycle": f"c{cycle_id}",
            "high_water_manifest_sha256": manifest["manifest_sha256"],
        }, backup, dict(manifest["backup"])

    def _registered_logs(self) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        snapshot, backup, expected = self._latest()
        fd = os.open(
            backup, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0))
        connection: Optional[sqlite3.Connection] = None
        try:
            info = os.fstat(fd)
            path_info = backup.lstat()
            if (not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                    or sg._hash_fd(fd, backup)
                    != (expected["sha256"], expected["bytes"])):
                raise StorageAssetError("latest snapshot query fd 与 manifest backup 漂移")
            connection = sqlite3.connect(
                f"file:{quote(f'/proc/self/fd/{fd}')}?mode=ro&immutable=1",
                uri=True)
            rows = connection.execute(
                "SELECT el.id,el.run_id,el.evaluation_attempt_id,el.cycle_id,"
                "el.log_kind,el.ref,el.content_hash,el.bytes,r.kind,r.cycle_id,ea.cycle_id "
                "FROM execution_log el LEFT JOIN run r ON r.id=el.run_id "
                "LEFT JOIN evaluation_attempt ea ON ea.id=el.evaluation_attempt_id "
                "ORDER BY el.id").fetchall()
        finally:
            if connection is not None:
                connection.close()
            os.close(fd)
        logs = []
        for row in rows:
            (log_id, run_id, attempt_id, cycle_id, log_kind, ref,
             content_hash, n_bytes, run_kind, run_cycle, attempt_cycle) = row
            if ((run_id is None) == (attempt_id is None)
                    or not isinstance(log_id, int) or log_id < 1
                    or not isinstance(cycle_id, int) or cycle_id < 1
                    or not isinstance(ref, str) or not ref or "\x00" in ref
                    or isinstance(n_bytes, bool) or not isinstance(n_bytes, int)
                    or n_bytes < 0):
                raise StorageAssetError(f"execution_log {log_id!r} 身份/bytes 非法")
            if run_id is not None:
                allowed = {"smoke", "stderr", "platform"}
                if run_kind in {"build", "exec"}:
                    allowed.add("train")
                if run_kind == "import":
                    allowed.add("import_clone")
                if run_cycle != cycle_id or log_kind not in allowed:
                    raise StorageAssetError(f"execution_log {log_id} run owner 闭包漂移")
            elif attempt_cycle != cycle_id or log_kind not in {"eval", "stderr", "platform"}:
                raise StorageAssetError(f"execution_log {log_id} attempt owner 闭包漂移")
            digest = _normal_hash(content_hash, label=f"execution_log {log_id} content_hash")
            try:
                source = resolve_registered_path(self.work_root, ref)
                relative_path = source.relative_to(self.work_root).as_posix()
                _registered_relative(
                    relative_path, label=f"execution_log {log_id}")
            except (RegisteredPathError, ValueError) as error:
                raise StorageAssetError(
                    f"execution_log {log_id} ref 越出 work_root/path-lineage") from error
            logs.append({
                "id": log_id, "run_id": run_id,
                "evaluation_attempt_id": attempt_id,
                "cycle_id": f"c{cycle_id}", "log_kind": log_kind,
                "ref": ref, "relative_path": relative_path,
                "content_hash": content_hash, "sha256": digest, "bytes": n_bytes,
                "source_path": source,
            })
        return snapshot, logs

    @staticmethod
    def _source_identity(log: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            key: log[key] for key in (
                "id", "run_id", "evaluation_attempt_id", "cycle_id", "log_kind",
                "ref", "relative_path", "content_hash", "sha256", "bytes")
        }

    def _open_original(self, log: Mapping[str, Any]) -> int:
        try:
            root = self.work_root.resolve(strict=True)
            parent = log["source_path"].parent.resolve(strict=True)
        except OSError as error:
            raise StorageAssetError(
                f"execution_log {log['id']} registered original 父目录缺失/不可解析") from error
        if parent != root and root not in parent.parents:
            raise StorageAssetError(
                f"execution_log {log['id']} registered original 经 symlink 越出 work_root")
        try:
            fd = os.open(
                log["source_path"], os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as error:
            raise StorageAssetError(
                f"execution_log {log['id']} registered original 缺失/不可打开") from error
        info = os.fstat(fd)
        try:
            path_info = log["source_path"].lstat()
        except OSError as error:
            os.close(fd)
            raise StorageAssetError(
                f"execution_log {log['id']} registered original 路径漂移") from error
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
                or info.st_size != log["bytes"]):
            os.close(fd)
            raise StorageAssetError(
                f"execution_log {log['id']} original 类型/link/bytes 漂移")
        return fd

    def _verify_original(self, log: Mapping[str, Any]) -> None:
        fd = self._open_original(log)
        try:
            if sg._hash_fd(fd, log["source_path"]) != (log["sha256"], log["bytes"]):
                raise StorageAssetError(
                    f"execution_log {log['id']} original hash/bytes 漂移")
        finally:
            os.close(fd)

    def _ensure_layout(self) -> None:
        for path in (
                self.root, self.root / "objects", self.objects, self.indexes):
            self.owner_guard()
            sg._ensure_dir(path)
            sg._sync_dir(path.parent)

    def _discard_temps(self) -> None:
        for directory, pattern in (
                (self.objects, _TEMP_RE), (self.indexes, _INDEX_TEMP_RE)):
            if not os.path.lexists(directory):
                continue
            changed = False
            for path in directory.iterdir():
                if pattern.fullmatch(path.name) is None:
                    continue
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise StorageAssetError(f"log mirror temp 类型非法: {path.name}")
                self.owner_guard()
                path.unlink()
                changed = True
            if changed:
                sg._sync_dir(directory)

    def _capacity(self, source_bytes: int) -> None:
        fs = os.statvfs(self.root)
        # Deflate stored-block worst overhead is small; budget a stricter bound plus
        # one MiB for CAS/index atomic publication.
        gzip_worst = source_bytes + ((source_bytes // 16384) + 1) * 5 + 64
        required_bytes = gzip_worst + _CAPACITY_MARGIN
        if (int(fs.f_bavail) * int(fs.f_frsize) < required_bytes
                or int(fs.f_favail) < 8):
            raise StorageAssetError(
                "log mirror 容量门拒绝: "
                f"required_bytes={required_bytes} required_inodes=8")

    def _confirm_durable_file(
            self, path: Path, *, expected_hash: str, expected_bytes: int,
            label: str) -> None:
        """Close rename-before-parent-fsync crash windows before dependent publication."""
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            path_info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o400
                    or (info.st_dev, info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                    or sg._hash_fd(fd, path) != (expected_hash, expected_bytes)):
                raise StorageAssetError(f"{label} durable identity 漂移")
            os.fsync(fd)
        finally:
            os.close(fd)
        self.owner_guard()
        sg._sync_dir(path.parent)

    def _compress(self, log: Mapping[str, Any], temporary: Path) -> tuple[str, int]:
        source_fd = self._open_original(log)
        output_fd = -1
        try:
            self._capacity(log["bytes"])
            output_fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o400)
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(os.dup(output_fd), "wb") as stream:
                with gzip.GzipFile(
                        filename="", mode="wb", compresslevel=9,
                        fileobj=stream, mtime=0) as compressed:
                    while True:
                        self.owner_guard()
                        block = os.read(source_fd, 1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        total += len(block)
                        compressed.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            if (digest.hexdigest(), total) != (log["sha256"], log["bytes"]):
                raise StorageAssetError(
                    f"execution_log {log['id']} original 在压缩同 fd 时漂移")
            os.fchmod(output_fd, 0o400)
            os.fsync(output_fd)
        finally:
            if output_fd >= 0:
                os.close(output_fd)
            os.close(source_fd)
        return sg._hash_file(temporary)

    @staticmethod
    def _mirror_value(log: Mapping[str, Any], *, digest: str, n_bytes: int) -> Dict[str, Any]:
        return {
            "schema": LOG_MIRROR_SCHEMA,
            "execution_log": RegisteredAssetArchive._source_identity(log),
            "mirror": {
                "codec": GZIP_PROFILE,
                "path": f"state/storage/log-mirrors/objects/sha256/{digest}.gz",
                "sha256": digest,
                "bytes": n_bytes,
            },
        }

    def _read_index(self, log_id: int) -> Optional[Dict[str, Any]]:
        path = self.indexes / f"execution-log-{log_id}.json"
        if not os.path.lexists(path):
            return None
        return sg._parse_json(sg._read(path), path)

    def _validate_index(self, log: Mapping[str, Any], value: Mapping[str, Any]) -> Dict[str, Any]:
        if (not isinstance(value, dict)
                or set(value) != {"schema", "execution_log", "mirror"}
                or value.get("schema") != LOG_MIRROR_SCHEMA
                or value.get("execution_log") != self._source_identity(log)
                or not isinstance(value.get("mirror"), dict)
                or set(value["mirror"]) != {"codec", "path", "sha256", "bytes"}
                or value["mirror"].get("codec") != GZIP_PROFILE
                or not isinstance(value["mirror"].get("sha256"), str)
                or _BARE_HASH_RE.fullmatch(value["mirror"]["sha256"]) is None
                or isinstance(value["mirror"].get("bytes"), bool)
                or not isinstance(value["mirror"].get("bytes"), int)
                or value["mirror"]["bytes"] < 1
                or value["mirror"].get("path") != (
                    "state/storage/log-mirrors/objects/sha256/"
                    f"{value['mirror'].get('sha256')}.gz")):
            raise StorageAssetError(f"execution_log {log['id']} mirror index 漂移")
        return dict(value["mirror"])

    def _verify_mirror(self, log: Mapping[str, Any], mirror: Mapping[str, Any]) -> None:
        path = self.work_root / mirror["path"]
        try:
            fd = os.open(
                path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise StorageAssetError(
                f"execution_log {log['id']} mirror object 缺失") from error
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o400
                    or info.st_size != mirror["bytes"]):
                raise StorageAssetError(
                    f"execution_log {log['id']} mirror 类型/link/bytes 漂移")
            compressed_hash = hashlib.sha256()
            compressed_bytes = 0
            header = bytearray()
            decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            raw_hash = hashlib.sha256()
            raw_bytes = 0
            ended = False
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                if ended:
                    raise StorageAssetError("gzip mirror 含尾随数据/多 member")
                compressed_hash.update(block)
                compressed_bytes += len(block)
                if len(header) < 10:
                    header.extend(block[:10 - len(header)])
                pending = block
                while pending:
                    limit = min(1024 * 1024, log["bytes"] - raw_bytes + 1)
                    if limit <= 0:
                        raise StorageAssetError("gzip mirror 解压超过登记 bytes")
                    before = len(pending)
                    output = decompressor.decompress(pending, limit)
                    raw_hash.update(output)
                    raw_bytes += len(output)
                    if raw_bytes > log["bytes"] or decompressor.unused_data:
                        raise StorageAssetError("gzip mirror 解压越界/含多 member")
                    pending = decompressor.unconsumed_tail
                    if len(pending) == before and not output:
                        raise StorageAssetError("gzip mirror 解压无进展")
                ended = decompressor.eof
            if (bytes(header) != _GZIP_HEADER or not decompressor.eof
                    or decompressor.unconsumed_tail or decompressor.unused_data
                    or compressed_hash.hexdigest() != mirror["sha256"]
                    or compressed_bytes != mirror["bytes"]
                    or raw_hash.hexdigest() != log["sha256"]
                    or raw_bytes != log["bytes"]):
                raise StorageAssetError(
                    f"execution_log {log['id']} mirror gzip/hash/bytes 漂移")
        except zlib.error as error:
            raise StorageAssetError(
                f"execution_log {log['id']} mirror gzip 损坏") from error
        finally:
            os.close(fd)

    def _scan_layout(self) -> tuple[Dict[int, Path], Dict[str, Path]]:
        if not os.path.lexists(self.root):
            return {}, {}
        for path in (
                self.root, self.root / "objects", self.objects,
                self.root / "indexes", self.indexes):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise StorageAssetError(f"log mirror 布局非法: {path}")
        indexes: Dict[int, Path] = {}
        for path in self.indexes.iterdir():
            match = _INDEX_RE.fullmatch(path.name)
            if match is None:
                if _INDEX_TEMP_RE.fullmatch(path.name) is not None:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        continue
                raise StorageAssetError(f"log mirror indexes 含非法条目: {path.name}")
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_nlink != 1):
                raise StorageAssetError(f"log mirror indexes 含非法条目: {path.name}")
            if stat.S_IMODE(info.st_mode) != 0o400:
                raise StorageAssetError(f"log mirror index mode 漂移: {path.name}")
            indexes[int(match.group(1))] = path
        objects: Dict[str, Path] = {}
        for path in self.objects.iterdir():
            match = _OBJECT_RE.fullmatch(path.name)
            if match is None:
                if _TEMP_RE.fullmatch(path.name) is not None:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        continue
                raise StorageAssetError(f"log mirror objects 含非法条目: {path.name}")
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_nlink != 1):
                raise StorageAssetError(f"log mirror object 类型非法: {path.name}")
            if stat.S_IMODE(info.st_mode) != 0o400:
                raise StorageAssetError(f"log mirror object mode 漂移: {path.name}")
            objects[match.group(1)] = path
        return indexes, objects

    def verify_log_mirrors(self) -> Dict[str, Any]:
        snapshot, logs = self._registered_logs()
        index_paths, objects = self._scan_layout()
        expected_ids = {item["id"] for item in logs}
        if set(index_paths) != expected_ids:
            missing = sorted(expected_ids - set(index_paths))
            extra = sorted(set(index_paths) - expected_ids)
            raise StorageAssetError(
                f"registered execution_log mirror index 闭包漂移: missing={missing} extra={extra}")
        referenced = set()
        for log in logs:
            self.owner_guard()
            self._verify_original(log)
            value = sg._parse_json(sg._read(index_paths[log["id"]]), index_paths[log["id"]])
            mirror = self._validate_index(log, value)
            self._verify_mirror(log, mirror)
            referenced.add(mirror["sha256"])
        for digest, path in objects.items():
            got_hash, _got_bytes = sg._hash_file(path)
            if got_hash != digest:
                raise StorageAssetError(f"orphan/linked log mirror CAS hash 漂移: {path.name}")
        self.owner_guard()
        return {
            "schema": LOG_MIRROR_REPORT_SCHEMA,
            "scope": "db_registered_execution_logs_only",
            **snapshot,
            "registered_logs": len(logs),
            "originals_verified": len(logs),
            "mirrors_verified": len(logs),
            "orphan_mirror_objects": sorted(set(objects) - referenced),
        }

    def mirror_logs(self) -> Dict[str, Any]:
        snapshot, logs = self._registered_logs()
        if not logs:
            verified = self.verify_log_mirrors()
            return {
                "schema": LOG_MIRROR_REPORT_SCHEMA,
                "scope": "db_registered_execution_logs_only",
                **snapshot, "registered_logs": 0, "published": [], "reused": [],
                "orphan_mirror_objects": verified["orphan_mirror_objects"],
            }
        self._ensure_layout()
        self._discard_temps()
        published = []
        reused = []
        for log in logs:
            self.owner_guard()
            existing = self._read_index(log["id"])
            if existing is not None:
                mirror = self._validate_index(log, existing)
                self._verify_original(log)
                self._verify_mirror(log, mirror)
                self._confirm_durable_file(
                    self.work_root / mirror["path"],
                    expected_hash=mirror["sha256"], expected_bytes=mirror["bytes"],
                    label=f"execution_log {log['id']} mirror object")
                index_raw = sg._canonical(existing)
                self._confirm_durable_file(
                    self.indexes / f"execution-log-{log['id']}.json",
                    expected_hash=hashlib.sha256(index_raw).hexdigest(),
                    expected_bytes=len(index_raw),
                    label=f"execution_log {log['id']} mirror index")
                reused.append(log["id"])
                continue
            temporary = self.objects / f".{uuid.uuid4().hex}.gz.tmp"
            try:
                digest, n_bytes = self._compress(log, temporary)
                destination = self.objects / f"{digest}.gz"
                self.owner_guard()
                if os.path.lexists(destination):
                    info = destination.lstat()
                    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o400
                            or sg._hash_file(destination) != (digest, n_bytes)):
                        raise StorageAssetError("log mirror CAS 既有对象漂移")
                    temporary.unlink()
                else:
                    os.replace(temporary, destination)
                    sg._sync_dir(self.objects)
                self._confirm_durable_file(
                    destination, expected_hash=digest, expected_bytes=n_bytes,
                    label=f"execution_log {log['id']} mirror object")
                value = self._mirror_value(log, digest=digest, n_bytes=n_bytes)
                self.owner_guard()
                sg._publish_once(
                    self.indexes / f"execution-log-{log['id']}.json",
                    sg._canonical(value))
                published.append(log["id"])
            finally:
                temporary.unlink(missing_ok=True)
        verified = self.verify_log_mirrors()
        return {
            "schema": LOG_MIRROR_REPORT_SCHEMA,
            "scope": "db_registered_execution_logs_only",
            **snapshot,
            "registered_logs": len(logs),
            "published": published,
            "reused": reused,
            "orphan_mirror_objects": verified["orphan_mirror_objects"],
        }

    # -- CP11.4c.3b.2b.3: registered checkpoints -------------------------

    def _registered_checkpoints(self) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        snapshot, backup, expected = self._latest()
        fd = os.open(
            backup, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0))
        connection: Optional[sqlite3.Connection] = None
        try:
            info = os.fstat(fd)
            path_info = backup.lstat()
            if (not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                    or sg._hash_fd(fd, backup)
                    != (expected["sha256"], expected["bytes"])):
                raise StorageAssetError(
                    "latest checkpoint snapshot query fd 与 manifest backup 漂移")
            connection = sqlite3.connect(
                f"file:{quote(f'/proc/self/fd/{fd}')}?mode=ro&immutable=1",
                uri=True)
            rows = connection.execute(
                "SELECT id,variant_id,ckpt_key,path,content_hash,hash_alg,"
                "artifact_type,origin,manifest_hash,source_uri,revision,produced_by_run "
                "FROM checkpoint ORDER BY id").fetchall()
        finally:
            if connection is not None:
                connection.close()
            os.close(fd)
        checkpoints = []
        for row in rows:
            (checkpoint_id, variant_id, key, ref, content_hash, hash_alg,
             artifact_type, origin, manifest_hash, source_uri, revision,
             produced_by_run) = row
            if (isinstance(checkpoint_id, bool) or not isinstance(checkpoint_id, int)
                    or checkpoint_id < 1 or isinstance(variant_id, bool)
                    or not isinstance(variant_id, int) or variant_id < 1
                    or not isinstance(key, str) or not key or "\x00" in key
                    or not isinstance(ref, str) or not ref or "\x00" in ref
                    or hash_alg != "sha256"
                    or artifact_type not in {
                        "checkpoint", "external_model", "prompt_only",
                        "algorithm", "retrieval_index"}
                    or origin not in {"run_produced", "external_import", "none"}):
                raise StorageAssetError(
                    f"checkpoint {checkpoint_id!r} registered identity 非法")
            digest = _normal_hash(
                content_hash, label=f"checkpoint {checkpoint_id} content_hash")
            try:
                source = resolve_registered_path(self.work_root, ref)
                relative = source.relative_to(self.work_root).as_posix()
                _registered_relative(
                    relative, label=f"checkpoint {checkpoint_id}")
            except (RegisteredPathError, ValueError) as error:
                raise StorageAssetError(
                    f"checkpoint {checkpoint_id} ref 越出 work_root/path-lineage") from error
            checkpoints.append({
                "id": checkpoint_id, "variant_id": variant_id,
                "ckpt_key": key, "ref": ref, "relative_path": relative,
                "content_hash": content_hash, "sha256": digest,
                "hash_alg": hash_alg, "artifact_type": artifact_type,
                "origin": origin, "manifest_hash": manifest_hash,
                "source_uri": source_uri, "revision": revision,
                "produced_by_run": produced_by_run, "source_path": source,
            })
        return snapshot, checkpoints

    @staticmethod
    def _checkpoint_identity(checkpoint: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            key: checkpoint[key] for key in (
                "id", "variant_id", "ckpt_key", "ref", "relative_path",
                "content_hash", "sha256", "hash_alg", "artifact_type",
                "origin", "manifest_hash", "source_uri", "revision",
                "produced_by_run")
        }

    def _open_checkpoint_original(
            self, checkpoint: Mapping[str, Any], *, expected_bytes: Optional[int] = None) -> int:
        source = checkpoint["source_path"]
        try:
            root = self.work_root.resolve(strict=True)
            parent = source.parent.resolve(strict=True)
        except OSError as error:
            raise StorageAssetError(
                f"checkpoint {checkpoint['id']} original 父目录缺失/不可解析") from error
        if parent != root and root not in parent.parents:
            raise StorageAssetError(
                f"checkpoint {checkpoint['id']} original 经 symlink 越出 work_root")
        try:
            fd = os.open(
                source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as error:
            raise StorageAssetError(
                f"checkpoint {checkpoint['id']} original 缺失/不可打开") from error
        try:
            info = os.fstat(fd)
            path_info = source.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_dev, info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                    or (expected_bytes is not None
                        and info.st_size != expected_bytes)):
                raise StorageAssetError(
                    f"checkpoint {checkpoint['id']} original 类型/link/bytes 漂移")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _verify_checkpoint_original(
            self, checkpoint: Mapping[str, Any], *, expected_bytes: int) -> None:
        fd = self._open_checkpoint_original(
            checkpoint, expected_bytes=expected_bytes)
        try:
            if sg._hash_fd(fd, checkpoint["source_path"]) != (
                    checkpoint["sha256"], expected_bytes):
                raise StorageAssetError(
                    f"checkpoint {checkpoint['id']} original hash/bytes 漂移")
        finally:
            os.close(fd)

    def _ensure_checkpoint_layout(self) -> None:
        for path in (
                self.checkpoint_root, self.checkpoint_root / "objects",
                self.checkpoint_objects, self.checkpoint_indexes):
            self.owner_guard()
            sg._ensure_dir(path)
            sg._sync_dir(path.parent)

    def _discard_checkpoint_temps(self) -> None:
        for directory, pattern in (
                (self.checkpoint_objects, _CHECKPOINT_TEMP_RE),
                (self.checkpoint_indexes, _CHECKPOINT_INDEX_TEMP_RE)):
            if not os.path.lexists(directory):
                continue
            changed = False
            for path in directory.iterdir():
                if pattern.fullmatch(path.name) is None:
                    continue
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise StorageAssetError(
                        f"checkpoint mirror temp 类型非法: {path.name}")
                self.owner_guard()
                path.unlink()
                changed = True
            if changed:
                sg._sync_dir(directory)

    def _copy_checkpoint_object(
            self, checkpoint: Mapping[str, Any], temporary: Path) -> int:
        source_fd = self._open_checkpoint_original(checkpoint)
        output_fd = -1
        try:
            source_info = os.fstat(source_fd)
            fs = os.statvfs(self.checkpoint_root)
            required = source_info.st_size + _CAPACITY_MARGIN
            if (int(fs.f_bavail) * int(fs.f_frsize) < required
                    or int(fs.f_favail) < 8):
                raise StorageAssetError(
                    "checkpoint mirror 容量门拒绝: "
                    f"required_bytes={required} required_inodes=8")
            output_fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o400)
            digest = hashlib.sha256()
            total = 0
            while True:
                self.owner_guard()
                block = os.read(source_fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                total += len(block)
                offset = 0
                while offset < len(block):
                    offset += os.write(output_fd, block[offset:])
            if digest.hexdigest() != checkpoint["sha256"]:
                raise StorageAssetError(
                    f"checkpoint {checkpoint['id']} original 在镜像同 fd 时 hash 漂移")
            os.fchmod(output_fd, 0o400)
            os.fsync(output_fd)
            return total
        finally:
            if output_fd >= 0:
                os.close(output_fd)
            os.close(source_fd)

    @staticmethod
    def _checkpoint_mirror_value(
            checkpoint: Mapping[str, Any], *, n_bytes: int) -> Dict[str, Any]:
        digest = checkpoint["sha256"]
        return {
            "schema": CHECKPOINT_MIRROR_SCHEMA,
            "checkpoint": RegisteredAssetArchive._checkpoint_identity(checkpoint),
            "mirror": {
                "codec": "identity/v1",
                "path": (
                    "state/storage/checkpoint-mirrors/objects/sha256/"
                    f"{digest}"),
                "sha256": digest,
                "bytes": n_bytes,
            },
        }

    def _read_checkpoint_index(self, checkpoint_id: int) -> Optional[Dict[str, Any]]:
        path = self.checkpoint_indexes / f"checkpoint-{checkpoint_id}.json"
        if not os.path.lexists(path):
            return None
        return sg._parse_json(sg._read(path), path)

    def _validate_checkpoint_index(
            self, checkpoint: Mapping[str, Any], value: Mapping[str, Any]) -> Dict[str, Any]:
        mirror = value.get("mirror") if isinstance(value, dict) else None
        if (not isinstance(value, dict)
                or set(value) != {"schema", "checkpoint", "mirror"}
                or value.get("schema") != CHECKPOINT_MIRROR_SCHEMA
                or value.get("checkpoint") != self._checkpoint_identity(checkpoint)
                or not isinstance(mirror, dict)
                or set(mirror) != {"codec", "path", "sha256", "bytes"}
                or mirror.get("codec") != "identity/v1"
                or mirror.get("sha256") != checkpoint["sha256"]
                or not isinstance(mirror.get("sha256"), str)
                or _BARE_HASH_RE.fullmatch(mirror["sha256"]) is None
                or isinstance(mirror.get("bytes"), bool)
                or not isinstance(mirror.get("bytes"), int)
                or mirror["bytes"] < 0
                or mirror.get("path") != (
                    "state/storage/checkpoint-mirrors/objects/sha256/"
                    f"{mirror.get('sha256')}")):
            raise StorageAssetError(
                f"checkpoint {checkpoint['id']} mirror index 漂移")
        return dict(mirror)

    def _verify_checkpoint_mirror(
            self, checkpoint: Mapping[str, Any], mirror: Mapping[str, Any]) -> None:
        path = self.work_root / mirror["path"]
        try:
            fd = os.open(
                path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise StorageAssetError(
                f"checkpoint {checkpoint['id']} mirror object 缺失") from error
        try:
            info = os.fstat(fd)
            path_info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o400
                    or (info.st_dev, info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                    or sg._hash_fd(fd, path) != (
                        checkpoint["sha256"], mirror["bytes"])):
                raise StorageAssetError(
                    f"checkpoint {checkpoint['id']} mirror 类型/hash/bytes 漂移")
        finally:
            os.close(fd)

    def _scan_checkpoint_layout(self) -> tuple[Dict[int, Path], Dict[str, Path]]:
        if not os.path.lexists(self.checkpoint_root):
            return {}, {}
        for path in (
                self.checkpoint_root, self.checkpoint_root / "objects",
                self.checkpoint_objects, self.checkpoint_indexes):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise StorageAssetError(f"checkpoint mirror 布局非法: {path}")
        indexes: Dict[int, Path] = {}
        for path in self.checkpoint_indexes.iterdir():
            match = _CHECKPOINT_INDEX_RE.fullmatch(path.name)
            if match is None:
                if _CHECKPOINT_INDEX_TEMP_RE.fullmatch(path.name) is not None:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        continue
                raise StorageAssetError(
                    f"checkpoint mirror indexes 含非法条目: {path.name}")
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o400):
                raise StorageAssetError(
                    f"checkpoint mirror index authority 漂移: {path.name}")
            indexes[int(match.group(1))] = path
        objects: Dict[str, Path] = {}
        for path in self.checkpoint_objects.iterdir():
            if _CHECKPOINT_OBJECT_RE.fullmatch(path.name) is None:
                if _CHECKPOINT_TEMP_RE.fullmatch(path.name) is not None:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        continue
                raise StorageAssetError(
                    f"checkpoint mirror objects 含非法条目: {path.name}")
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o400):
                raise StorageAssetError(
                    f"checkpoint mirror object authority 漂移: {path.name}")
            objects[path.name] = path
        return indexes, objects

    def _checkpoint_closure(
            self, *, verify_originals: bool) -> tuple[
                Dict[str, Any], List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        snapshot, checkpoints = self._registered_checkpoints()
        index_paths, objects = self._scan_checkpoint_layout()
        expected_ids = {item["id"] for item in checkpoints}
        if set(index_paths) != expected_ids:
            raise StorageAssetError(
                "registered checkpoint mirror index 闭包漂移: "
                f"missing={sorted(expected_ids - set(index_paths))} "
                f"extra={sorted(set(index_paths) - expected_ids)}")
        mirrors = {}
        referenced = set()
        for checkpoint in checkpoints:
            self.owner_guard()
            index = sg._parse_json(
                sg._read(index_paths[checkpoint["id"]]),
                index_paths[checkpoint["id"]])
            mirror = self._validate_checkpoint_index(checkpoint, index)
            self._verify_checkpoint_mirror(checkpoint, mirror)
            if verify_originals:
                self._verify_checkpoint_original(
                    checkpoint, expected_bytes=mirror["bytes"])
            mirrors[checkpoint["id"]] = mirror
            referenced.add(mirror["sha256"])
        for digest, path in objects.items():
            got_hash, _got_bytes = sg._hash_file(path)
            if got_hash != digest:
                raise StorageAssetError(
                    f"orphan/linked checkpoint mirror CAS hash 漂移: {path.name}")
        self.owner_guard()
        return snapshot, checkpoints, mirrors

    def verify_checkpoint_mirrors(self) -> Dict[str, Any]:
        snapshot, checkpoints, _mirrors = self._checkpoint_closure(
            verify_originals=True)
        _indexes, objects = self._scan_checkpoint_layout()
        referenced = {
            checkpoint["sha256"] for checkpoint in checkpoints}
        return {
            "schema": CHECKPOINT_MIRROR_REPORT_SCHEMA,
            "scope": "db_registered_checkpoints_only",
            **snapshot,
            "registered_checkpoints": len(checkpoints),
            "originals_verified": len(checkpoints),
            "mirrors_verified": len(checkpoints),
            "orphan_mirror_objects": sorted(set(objects) - referenced),
        }

    def mirror_checkpoints(self) -> Dict[str, Any]:
        snapshot, checkpoints = self._registered_checkpoints()
        if checkpoints:
            self._ensure_checkpoint_layout()
            self._discard_checkpoint_temps()
        published = []
        reused = []
        for checkpoint in checkpoints:
            self.owner_guard()
            existing = self._read_checkpoint_index(checkpoint["id"])
            if existing is not None:
                mirror = self._validate_checkpoint_index(checkpoint, existing)
                self._verify_checkpoint_original(
                    checkpoint, expected_bytes=mirror["bytes"])
                self._verify_checkpoint_mirror(checkpoint, mirror)
                self._confirm_durable_file(
                    self.work_root / mirror["path"],
                    expected_hash=mirror["sha256"],
                    expected_bytes=mirror["bytes"],
                    label=f"checkpoint {checkpoint['id']} mirror object")
                index_raw = sg._canonical(existing)
                self._confirm_durable_file(
                    self.checkpoint_indexes / f"checkpoint-{checkpoint['id']}.json",
                    expected_hash=hashlib.sha256(index_raw).hexdigest(),
                    expected_bytes=len(index_raw),
                    label=f"checkpoint {checkpoint['id']} mirror index")
                reused.append(checkpoint["id"])
                continue
            temporary = self.checkpoint_objects / f".{uuid.uuid4().hex}.tmp"
            try:
                n_bytes = self._copy_checkpoint_object(checkpoint, temporary)
                destination = self.checkpoint_objects / checkpoint["sha256"]
                self.owner_guard()
                if os.path.lexists(destination):
                    info = destination.lstat()
                    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                            or info.st_nlink != 1
                            or stat.S_IMODE(info.st_mode) != 0o400
                            or sg._hash_file(destination) != (
                                checkpoint["sha256"], n_bytes)):
                        raise StorageAssetError(
                            "checkpoint mirror CAS 既有对象漂移")
                    temporary.unlink()
                else:
                    try:
                        os.link(temporary, destination, follow_symlinks=False)
                    except FileExistsError:
                        if sg._hash_file(destination) != (
                                checkpoint["sha256"], n_bytes):
                            raise StorageAssetError(
                                "checkpoint mirror CAS 并发对象漂移")
                    finally:
                        temporary.unlink(missing_ok=True)
                    sg._sync_dir(self.checkpoint_objects)
                self._confirm_durable_file(
                    destination, expected_hash=checkpoint["sha256"],
                    expected_bytes=n_bytes,
                    label=f"checkpoint {checkpoint['id']} mirror object")
                value = self._checkpoint_mirror_value(
                    checkpoint, n_bytes=n_bytes)
                self.owner_guard()
                sg._publish_once(
                    self.checkpoint_indexes / f"checkpoint-{checkpoint['id']}.json",
                    sg._canonical(value))
                published.append(checkpoint["id"])
            finally:
                temporary.unlink(missing_ok=True)
        verified = self.verify_checkpoint_mirrors()
        return {
            "schema": CHECKPOINT_MIRROR_REPORT_SCHEMA,
            "scope": "db_registered_checkpoints_only",
            **snapshot,
            "registered_checkpoints": len(checkpoints),
            "published": published,
            "reused": reused,
            "orphan_mirror_objects": verified["orphan_mirror_objects"],
        }

    # -- Combined registered set and hydration ----------------------------

    def mirror_registered_assets(self) -> Dict[str, Any]:
        checkpoints = self.mirror_checkpoints()
        logs = self.mirror_logs()
        if (checkpoints["high_water_cycle"] != logs["high_water_cycle"]
                or checkpoints["high_water_manifest_sha256"]
                != logs["high_water_manifest_sha256"]):
            raise StorageAssetError("registered asset mirror high-water 漂移")
        return {
            "schema": REGISTERED_SET_REPORT_SCHEMA,
            "scope": "db_registered_checkpoints_and_execution_logs",
            "high_water_cycle": checkpoints["high_water_cycle"],
            "high_water_manifest_sha256": checkpoints[
                "high_water_manifest_sha256"],
            "checkpoints": checkpoints,
            "execution_logs": logs,
        }

    def verify_registered_assets(self) -> Dict[str, Any]:
        checkpoints = self.verify_checkpoint_mirrors()
        logs = self.verify_log_mirrors()
        if (checkpoints["high_water_cycle"] != logs["high_water_cycle"]
                or checkpoints["high_water_manifest_sha256"]
                != logs["high_water_manifest_sha256"]):
            raise StorageAssetError("registered asset verify high-water 漂移")
        return {
            "schema": REGISTERED_SET_REPORT_SCHEMA,
            "scope": "db_registered_checkpoints_and_execution_logs",
            "high_water_cycle": checkpoints["high_water_cycle"],
            "high_water_manifest_sha256": checkpoints[
                "high_water_manifest_sha256"],
            "checkpoints": checkpoints,
            "execution_logs": logs,
        }

    def _log_closure_without_originals(self) -> tuple[
            Dict[str, Any], List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        snapshot, logs = self._registered_logs()
        index_paths, _objects = self._scan_layout()
        expected_ids = {item["id"] for item in logs}
        if set(index_paths) != expected_ids:
            raise StorageAssetError(
                "registered execution_log mirror index 闭包漂移: "
                f"missing={sorted(expected_ids - set(index_paths))} "
                f"extra={sorted(set(index_paths) - expected_ids)}")
        mirrors = {}
        for log in logs:
            value = sg._parse_json(
                sg._read(index_paths[log["id"]]), index_paths[log["id"]])
            mirror = self._validate_index(log, value)
            self._verify_mirror(log, mirror)
            mirrors[log["id"]] = mirror
        return snapshot, logs, mirrors

    @staticmethod
    def _selected_cycle_label(value: Optional[str | int]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, bool):
            raise StorageAssetError("registered restore cycle 须为 cN")
        if isinstance(value, int) and value > 0:
            return f"c{value}"
        if isinstance(value, str) and re.fullmatch(r"c[1-9][0-9]*", value):
            return value
        raise StorageAssetError("registered restore cycle 须为 cN")

    def registered_restore_authority(
            self, *, cycle: Optional[str | int] = None) -> Dict[str, Any]:
        checkpoint_snapshot, checkpoints, checkpoint_mirrors = (
            self._checkpoint_closure(verify_originals=False))
        log_snapshot, logs, log_mirrors = self._log_closure_without_originals()
        if checkpoint_snapshot != log_snapshot:
            raise StorageAssetError("registered restore mirror high-water 漂移")
        selected = self._selected_cycle_label(cycle)
        if (selected is not None
                and selected != checkpoint_snapshot["high_water_cycle"]):
            raise StorageAssetError(
                "registered restore 只接受 latest mirrored high-water")
        items = []
        for checkpoint in checkpoints:
            mirror = checkpoint_mirrors[checkpoint["id"]]
            items.append({
                "kind": "checkpoint", "owner_id": checkpoint["id"],
                "relative_path": checkpoint["relative_path"],
                "sha256": checkpoint["sha256"], "bytes": mirror["bytes"],
                "mirror_path": mirror["path"],
                "mirror_sha256": mirror["sha256"],
                "mirror_bytes": mirror["bytes"],
            })
        for log in logs:
            mirror = log_mirrors[log["id"]]
            items.append({
                "kind": "execution_log", "owner_id": log["id"],
                "relative_path": log["relative_path"],
                "sha256": log["sha256"], "bytes": log["bytes"],
                "mirror_path": mirror["path"],
                "mirror_sha256": mirror["sha256"],
                "mirror_bytes": mirror["bytes"],
            })
        items.sort(key=lambda item: (
            item["relative_path"], item["kind"], item["owner_id"]))
        identities: Dict[str, tuple[str, int]] = {}
        for item in items:
            _registered_relative(
                item["relative_path"], label="registered restore")
            identity = (item["sha256"], item["bytes"])
            prior = identities.setdefault(item["relative_path"], identity)
            if prior != identity:
                raise StorageAssetError(
                    "registered restore 同路径资产身份冲突")
        value = {
            "schema": REGISTERED_RESTORE_SCHEMA,
            "scope": "db_registered_checkpoints_and_execution_logs",
            "source_work_root": str(self.work_root),
            "source_cycle": checkpoint_snapshot["high_water_cycle"],
            "source_manifest_sha256": checkpoint_snapshot[
                "high_water_manifest_sha256"],
            "files": items,
        }
        if len(sg._canonical(value)) > _REGISTERED_RECEIPT_MAX_BYTES:
            raise StorageAssetError(
                "registered restore authority 超过 canonical receipt 上限")
        return value

    def verify_registered_restore_source(
            self, *, cycle: Optional[str | int] = None) -> Dict[str, Any]:
        value = self.registered_restore_authority(cycle=cycle)
        return {
            "high_water_cycle": value["source_cycle"],
            "high_water_manifest_sha256": value[
                "source_manifest_sha256"],
            "registered_checkpoints": sum(
                item["kind"] == "checkpoint" for item in value["files"]),
            "registered_execution_logs": sum(
                item["kind"] == "execution_log" for item in value["files"]),
        }

    @staticmethod
    def _restore_marker(target: Path) -> Optional[bytes]:
        try:
            return read_marker(target)
        except StorageRestoreContractError as error:
            raise StorageAssetError("registered restore marker 非法") from error

    def _validate_registered_restore_target(
            self, target: Path, snapshot: Mapping[str, Any]) -> Dict[str, Any]:
        receipt_path = target / "restore.json"
        try:
            receipt = sg._parse_json(sg._read(receipt_path), receipt_path)
        except (OSError, sg.StorageGovernanceError) as error:
            raise StorageAssetError("target 缺少合法 SQLite restore receipt") from error
        allowed = {
            "schema", "scope", "continuation_mode", "publication_contract",
            "source_work_root", "source_cycle", "source_manifest_sha256", "backup",
            "registered_path_roots",
        }
        required = allowed - {"registered_path_roots"}
        if (not required <= set(receipt) <= allowed
                or receipt.get("schema") != "meta-research-storage-restore/v1"
                or receipt.get("scope") != "sqlite_truth_only"
                or receipt.get("continuation_mode")
                != "registered_asset_restore_required"
                or receipt.get("publication_contract")
                != "atomic_noreplace_or_lease_fenced_ready"
                or receipt.get("source_work_root") != str(self.work_root)
                or receipt.get("source_cycle") != snapshot["high_water_cycle"]
                or receipt.get("source_manifest_sha256")
                != snapshot["high_water_manifest_sha256"]
                or receipt.get("registered_path_roots", [str(self.work_root)])
                != [str(path) for path in registered_path_roots(self.work_root)]):
            raise StorageAssetError(
                "target SQLite restore receipt 不是 exact registered continuation/high-water")
        chain = self.snapshot_archive._chain(retain=3)
        selected = chain["ordered"][-1]
        manifest = chain["manifests"][-1]
        self.snapshot_archive.publisher._verify_backup_object(
            target / "research.sqlite",
            expected_hash=manifest["backup"]["sha256"],
            expected_bytes=manifest["backup"]["bytes"],
            cycle_id=selected, cycle_status=manifest["cycle_status"],
            allow_later_cycles=manifest["adoption_baseline"] is True)
        return receipt

    @staticmethod
    def _ensure_restore_parent(
            target: Path, relative: Path,
            target_guard: Callable[[], None]) -> Path:
        current = target
        for component in relative.parent.parts:
            current = current / component
            if os.path.lexists(current):
                info = current.lstat()
                if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_uid != os.geteuid() or info.st_mode & 0o022):
                    raise StorageAssetError(
                        f"registered restore parent authority 非法: {current}")
            else:
                target_guard()
                current.mkdir(mode=0o700)
                sg._sync_dir(current.parent)
        return target / relative

    @staticmethod
    def _verify_restored_file(
            path: Path, *, expected_hash: str, expected_bytes: int) -> None:
        try:
            fd = os.open(
                path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise StorageAssetError(f"registered restore target 缺失: {path}") from error
        try:
            info = os.fstat(fd)
            path_info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o400
                    or (info.st_dev, info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                    or sg._hash_fd(fd, path) != (expected_hash, expected_bytes)):
                raise StorageAssetError(
                    f"registered restore target 身份漂移: {path}")
        finally:
            os.close(fd)

    def _copy_checkpoint_restore(
            self, item: Mapping[str, Any], output_fd: int) -> None:
        source = self.work_root / item["mirror_path"]
        try:
            fd = os.open(
                source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise StorageAssetError(
                "checkpoint mirror restore 不可打开/缺失") from error
        try:
            digest = hashlib.sha256()
            total = 0
            while True:
                self.owner_guard()
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                total += len(block)
                offset = 0
                while offset < len(block):
                    offset += os.write(output_fd, block[offset:])
            if (digest.hexdigest(), total) != (item["sha256"], item["bytes"]):
                raise StorageAssetError("checkpoint mirror 在 restore 同 fd 时漂移")
        finally:
            os.close(fd)

    def _copy_log_restore(
            self, item: Mapping[str, Any], output_fd: int) -> None:
        source = self.work_root / item["mirror_path"]
        try:
            fd = os.open(
                source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise StorageAssetError(
                "log mirror restore 不可打开/缺失") from error
        try:
            compressed_hash = hashlib.sha256()
            compressed_bytes = 0
            raw_hash = hashlib.sha256()
            raw_bytes = 0
            decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            ended = False
            while True:
                self.owner_guard()
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                if ended:
                    raise StorageAssetError("log mirror restore 含尾随 member")
                compressed_hash.update(block)
                compressed_bytes += len(block)
                pending = block
                while pending:
                    limit = min(1024 * 1024, item["bytes"] - raw_bytes + 1)
                    if limit <= 0:
                        raise StorageAssetError("log mirror restore 解压越界")
                    before = len(pending)
                    output = decompressor.decompress(pending, limit)
                    raw_hash.update(output)
                    raw_bytes += len(output)
                    offset = 0
                    while offset < len(output):
                        offset += os.write(output_fd, output[offset:])
                    if raw_bytes > item["bytes"] or decompressor.unused_data:
                        raise StorageAssetError("log mirror restore 解压越界/多 member")
                    pending = decompressor.unconsumed_tail
                    if len(pending) == before and not output:
                        raise StorageAssetError("log mirror restore 解压无进展")
                ended = decompressor.eof
            if (not decompressor.eof or decompressor.unused_data
                    or decompressor.unconsumed_tail
                    or compressed_hash.hexdigest() != item["mirror_sha256"]
                    or compressed_bytes != item["mirror_bytes"]
                    or raw_hash.hexdigest() != item["sha256"]
                    or raw_bytes != item["bytes"]):
                raise StorageAssetError("log mirror restore gzip/hash/bytes 漂移")
        except zlib.error as error:
            raise StorageAssetError("log mirror restore gzip 损坏") from error
        finally:
            os.close(fd)

    def _restore_one_file(
            self, target: Path, item: Mapping[str, Any],
            target_guard: Callable[[], None]) -> bool:
        relative = Path(item["relative_path"])
        destination = self._ensure_restore_parent(target, relative, target_guard)
        if os.path.lexists(destination):
            self._verify_restored_file(
                destination, expected_hash=item["sha256"],
                expected_bytes=item["bytes"])
            return False
        prefix = f".{destination.name}.registered-restore-"
        changed = False
        for path in destination.parent.iterdir():
            if not path.name.startswith(prefix) or not path.name.endswith(".tmp"):
                continue
            token = path.name[len(prefix):-4]
            if _RESTORE_TEMP_SUFFIX_RE.fullmatch(token) is None:
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise StorageAssetError("registered restore temp 类型非法")
            target_guard()
            path.unlink()
            changed = True
        if changed:
            sg._sync_dir(destination.parent)
        temporary = destination.parent / (
            f".{destination.name}.registered-restore-{uuid.uuid4().hex}.tmp")
        output_fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o400)
        try:
            try:
                if item["kind"] == "checkpoint":
                    self._copy_checkpoint_restore(item, output_fd)
                else:
                    self._copy_log_restore(item, output_fd)
                os.fchmod(output_fd, 0o400)
                os.fsync(output_fd)
            finally:
                os.close(output_fd)
            target_guard()
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                self._verify_restored_file(
                    destination, expected_hash=item["sha256"],
                    expected_bytes=item["bytes"])
                return False
            temporary.unlink()
            sg._sync_dir(destination.parent)
            self._verify_restored_file(
                destination, expected_hash=item["sha256"],
                expected_bytes=item["bytes"])
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def _publish_restore_receipt(
            self, path: Path, value: Mapping[str, Any],
            target_guard: Callable[[], None]) -> None:
        self.owner_guard()
        target_guard()
        sg._publish_once(path, sg._canonical(value))
        self._verify_restored_file(
            path, expected_hash=sg._hash_bytes(sg._canonical(value)),
            expected_bytes=len(sg._canonical(value)))
        sg._sync_dir(path.parent)

    def restore_registered_assets(
            self, *, target: Path | str,
            cycle: Optional[str | int] = None) -> Dict[str, Any]:
        value = self.registered_restore_authority(cycle=cycle)
        snapshot = {
            "high_water_cycle": value["source_cycle"],
            "high_water_manifest_sha256": value[
                "source_manifest_sha256"],
        }
        items = value["files"]
        target_path = Path(os.path.abspath(os.fspath(target)))
        try:
            resolved_target = target_path.resolve(strict=True)
        except OSError as error:
            raise StorageAssetError("registered restore target 不可解析") from error
        if resolved_target != target_path:
            raise StorageAssetError("registered restore target 含 symlink/alias")
        target_path = resolved_target
        try:
            validate_restore_target_lineage(self.work_root, target_path)
        except RegisteredPathError as error:
            raise StorageAssetError(
                "registered restore target 与 path-lineage 不得相等/嵌套") from error
        completion = target_path / REGISTERED_COMPLETION_RELATIVE
        marker_value = self._restore_marker(target_path)
        if marker_value not in (None, REGISTERED_RESTORE_MARKER):
            raise StorageAssetError(
                "registered continuation 不接受 import-only restore marker")
        if marker_value is None and not os.path.lexists(completion):
            raise StorageAssetError(
                "registered restore target 缺 continuation marker/completion")
        target_lease = InstanceLease.acquire(
            target_path,
            expected_restore_marker=marker_value)
        primary: Optional[BaseException] = None
        try:
            target_guard = target_lease.assert_owned
            target_guard()
            self._validate_registered_restore_target(
                target_path, snapshot)
            unique = {}
            for item in items:
                unique.setdefault(item["relative_path"], item)
            missing = [
                item for relative, item in unique.items()
                if not os.path.lexists(target_path / relative)]
            fs = os.statvfs(target_path)
            required_bytes = sum(item["bytes"] for item in missing)
            required_inodes = (
                _RESTORE_CAPACITY_MARGIN_INODES
                + len(missing)
                + sum(len(Path(item["relative_path"]).parent.parts)
                      for item in missing))
            if (int(fs.f_bavail) * int(fs.f_frsize)
                    < required_bytes + _RESTORE_CAPACITY_MARGIN_BYTES
                    or int(fs.f_favail) < required_inodes):
                raise StorageAssetError(
                    "registered restore 容量门拒绝: "
                    f"required_bytes={required_bytes + _RESTORE_CAPACITY_MARGIN_BYTES} "
                    f"required_inodes={required_inodes}")
            published = 0
            reused = 0
            for item in unique.values():
                self.owner_guard()
                target_guard()
                if self._restore_one_file(target_path, item, target_guard):
                    published += 1
                else:
                    reused += 1
            self._ensure_restore_parent(
                target_path, Path("state/storage/registered-assets/restore.json"),
                target_guard)
            self._publish_restore_receipt(completion, value, target_guard)
            try:
                validate_registered_completion(
                    target_path, source_work_root=str(self.work_root),
                    source_cycle=value["source_cycle"],
                    source_manifest_sha256=value[
                        "source_manifest_sha256"])
            except StorageRestoreContractError as error:
                raise StorageAssetError(
                    "registered restore completion 自验失败") from error
            for item in unique.values():
                self._verify_restored_file(
                    target_path / item["relative_path"],
                    expected_hash=item["sha256"], expected_bytes=item["bytes"])
            return {
                "schema": REGISTERED_RESTORE_REPORT_SCHEMA,
                "scope": value["scope"],
                "source_cycle": value["source_cycle"],
                "source_manifest_sha256": value["source_manifest_sha256"],
                "hydrated_checkpoints": sum(
                    item["kind"] == "checkpoint" for item in items),
                "hydrated_execution_logs": sum(
                    item["kind"] == "execution_log" for item in items),
                "published": published, "reused": reused,
                "completion_receipt": completion.relative_to(
                    target_path).as_posix(),
            }
        except BaseException as error:
            primary = error
            raise
        finally:
            close_error = target_lease.close()
            if close_error is not None:
                if primary is None:
                    raise close_error
                add_note = getattr(primary, "add_note", None)
                if callable(add_note):
                    add_note(
                        f"registered restore target lease close 失败: {close_error}")
