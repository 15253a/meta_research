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
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote

from . import storage_governance as sg


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


class StorageAssetError(sg.StorageGovernanceError):
    """Registered asset or its immutable mirror is incomplete/corrupt."""


def _normal_hash(value: Any, *, label: str) -> str:
    match = _HASH_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise StorageAssetError(f"{label} 不是规范 SHA256")
    return match.group(1)


class RegisteredAssetArchive:
    """Offline registered-asset primitive; caller supplies a lease-fenced SnapshotArchive."""

    def __init__(self, snapshot_archive):  # noqa: ANN001 - avoid a storage_ops import cycle
        self.snapshot_archive = snapshot_archive
        self.work_root = snapshot_archive.work_root
        self.owner_guard = snapshot_archive.owner_guard
        self.root = snapshot_archive.publisher.storage_root / "log-mirrors"
        self.objects = self.root / "objects" / "sha256"
        self.indexes = self.root / "indexes"

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
            raw_path = Path(ref)
            source = Path(os.path.abspath(
                os.fspath(raw_path if raw_path.is_absolute() else self.work_root / raw_path)))
            if source == self.work_root or self.work_root not in source.parents:
                raise StorageAssetError(f"execution_log {log_id} ref 越出 work_root")
            logs.append({
                "id": log_id, "run_id": run_id,
                "evaluation_attempt_id": attempt_id,
                "cycle_id": f"c{cycle_id}", "log_kind": log_kind,
                "ref": ref, "relative_path": source.relative_to(self.work_root).as_posix(),
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
