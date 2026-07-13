"""Canonical, read-only evidence packs for a completed recovery probe.

This module is deliberately not another restore engine.  Operators keep using
``storage_ops restore-with-import-materializations`` and the existing
``orchestrator.run`` entry point.  Once both owners are stopped, ``pack``
freezes the already-verified source evidence and, optionally, proves one
successful post-restore research cycle.  ``verify`` reads only the pack.

The v1 claim is intentionally narrow: pack byte integrity and an optional
``one_cycle_resume_probe``.  Registered checkpoints/log originals and a full
work-root disaster-recovery closure are not claimed.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import quote

from . import database
from . import storage_governance as sg
from .fault_schedule import verify_fault_schedule
from .import_materialization_contract import (
    canonical_hash,
    execution_contract,
    spec_ledger,
)
from .instance_lease import (
    RESTORE_IN_PROGRESS_NAME,
    InstanceLease,
    restore_parent_claim_name,
)
from .qualification_firewall import load_qualification_firewall
from .shared_fs_canary import LOCAL_SCOPE, TWO_NODE_SCOPE, verify_canary
from .storage_assets import (
    GZIP_PROFILE,
    LOG_MIRROR_SCHEMA,
    RegisteredAssetArchive,
)
from .storage_imports import ImportMaterializationArchive
from .storage_ops import SnapshotArchive


PACK_PROTOCOL = "meta-research-evidence-pack/v1"
READY_PROTOCOL = "meta-research-evidence-pack-ready/v1"
RESUME_PROTOCOL = "meta-research-one-cycle-resume-probe/v1"
PACK_SUFFIX = ".evidence"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CYCLE_RE = re.compile(r"^c([1-9][0-9]*)$")
_SCHEDULE_RE = re.compile(r"^[0-9a-f]{32}$")
_ITEM_KINDS = {
    "report", "storage_genesis", "storage_pointer", "storage_manifest",
    "sqlite_snapshot", "restore_receipt", "log_mirror_index",
    "log_mirror_object", "import_index", "import_repository_file",
    "dependency_image_file", "fault_receipt", "qualification_receipt",
    "canary_receipt",
}
_JSON_ITEM_KINDS = {
    "report", "storage_genesis", "storage_pointer", "storage_manifest",
    "restore_receipt", "log_mirror_index", "import_index", "fault_receipt",
    "qualification_receipt", "canary_receipt",
}
_MAX_JSON_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_FILES = 200_000
DEFAULT_MAX_BYTES = 256 * 1024 * 1024 * 1024
_MAX_SINGLE_OBJECT_BYTES = 64 * 1024 * 1024 * 1024
_COPY_BLOCK = 1024 * 1024


class EvidencePackError(RuntimeError):
    """The requested pack or its evidence closure is unsafe/incomplete."""


def _canonical(value: Any) -> bytes:
    try:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False)
    except (TypeError, ValueError, OverflowError) as error:
        raise EvidencePackError("evidence JSON 含非规范值") from error
    return (text + "\n").encode("utf-8")


def _strict_json_value(raw: bytes, *, label: str,
                       maximum: int = _MAX_JSON_BYTES) -> Any:
    if not isinstance(raw, bytes) or not 2 <= len(raw) <= maximum:
        raise EvidencePackError(f"{label} JSON 大小非法")

    def unique(pairs):  # noqa: ANN001
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key: {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite {token}")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError,
            RecursionError) as error:
        raise EvidencePackError(f"{label} 不是严格 JSON") from error
    if _canonical(value) != raw:
        raise EvidencePackError(f"{label} 不是 canonical JSON")
    return value


def _strict_json(raw: bytes, *, label: str,
                 maximum: int = _MAX_JSON_BYTES) -> Dict[str, Any]:
    value = _strict_json_value(raw, label=label, maximum=maximum)
    if not isinstance(value, dict):
        raise EvidencePackError(f"{label} 不是 JSON object")
    return value


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
        info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink,
    )


def _cycle(value: Any, *, label: str) -> int:
    match = _CYCLE_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise EvidencePackError(f"{label} cycle 非法")
    return int(match.group(1))


def _logical_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise EvidencePackError("evidence logical_id 非法")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise EvidencePackError("evidence logical_id 非 UTF-8") from error
    if (len(encoded) > 8192 or "\\" in value
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)):
        raise EvidencePackError("evidence logical_id 非法")
    pure = PurePosixPath(value)
    parts = value.split("/")
    if (pure.is_absolute() or pure.as_posix() != value or len(parts) > 160
            or any(segment in {"", ".", ".."} for segment in parts)):
        raise EvidencePackError(f"evidence logical_id 越界: {value!r}")
    return value


def _safe_directory(path: Path, *, label: str, private: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise EvidencePackError(f"{label} 缺失/不可读: {path}") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o022
            or (private and stat.S_IMODE(info.st_mode) != 0o700)):
        raise EvidencePackError(f"{label} authority 非法: {path}")
    return info


def _canonical_directory(path: Path, *, label: str, private: bool = False) -> None:
    _safe_directory(path, label=label, private=private)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise EvidencePackError(f"{label} 不可解析") from error
    if resolved != path:
        raise EvidencePackError(f"{label} 不得经 symlink ancestor 转向")


def _read_regular(path: Path, *, label: str,
                  maximum: int = _MAX_JSON_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise EvidencePackError(f"{label} 不可安全打开") from error
    try:
        before = os.fstat(fd)
        try:
            path_info = path.lstat()
        except OSError as error:
            raise EvidencePackError(f"{label} 路径身份丢失") from error
        if (not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_mode & 0o022
                or (before.st_dev, before.st_ino)
                != (path_info.st_dev, path_info.st_ino)
                or before.st_size < 0 or before.st_size > maximum):
            raise EvidencePackError(f"{label} authority/大小非法")
        raw = bytearray()
        while len(raw) < before.st_size:
            block = os.read(fd, min(_COPY_BLOCK, before.st_size - len(raw)))
            if not block:
                raise EvidencePackError(f"{label} 提前 EOF")
            raw.extend(block)
        after = os.fstat(fd)
        final_path = path.lstat()
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if (identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (after.st_dev, after.st_ino)
                != (final_path.st_dev, final_path.st_ino)):
            raise EvidencePackError(f"{label} 读取期间漂移")
        return bytes(raw)
    finally:
        os.close(fd)


def _verify_pack_file(
        path: Path, *, label: str, expected_hash: str, expected_bytes: int,
        expected_mode: int = 0o400) -> tuple[int, ...]:
    """Stream one immutable pack file; never materialize large objects in RAM."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise EvidencePackError(f"{label} 不可安全打开") from error
    try:
        before = os.fstat(fd)
        path_before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != expected_mode
                or before.st_size != expected_bytes
                or (before.st_dev, before.st_ino)
                != (path_before.st_dev, path_before.st_ino)):
            raise EvidencePackError(f"{label} authority/bytes 非法")
        digest = hashlib.sha256()
        seen = 0
        while seen < expected_bytes:
            block = os.read(fd, min(_COPY_BLOCK, expected_bytes - seen))
            if not block:
                raise EvidencePackError(f"{label} 提前 EOF")
            digest.update(block)
            seen += len(block)
        if os.read(fd, 1):
            raise EvidencePackError(f"{label} 含未声明尾随 bytes")
        after = os.fstat(fd)
        path_after = path.lstat()
        identity = _file_identity(before)
        if (seen != expected_bytes or digest.hexdigest() != expected_hash
                or identity != _file_identity(after)
                or (after.st_dev, after.st_ino)
                != (path_after.st_dev, path_after.st_ino)):
            raise EvidencePackError(f"{label} hash/bytes/identity 漂移")
        return identity
    finally:
        os.close(fd)


def _assert_pack_unchanged(
        pack: Path, identities: Mapping[str, tuple[int, ...]], *,
        root_fd: Optional[int] = None) -> None:
    """Cheap post-semantic seal; ctime catches in-place rewrites without rehashing."""
    if root_fd is None:
        _canonical_directory(pack, label="evidence pack", private=True)
        root_info = pack.lstat()
    else:
        root_info = os.fstat(root_fd)
        if (not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != os.geteuid()
                or stat.S_IMODE(root_info.st_mode) != 0o700):
            raise EvidencePackError("evidence pack pinned root authority 漂移")
    expected_root = {"manifest.json", "READY.json", "objects"}
    if {path.name for path in pack.iterdir()} != expected_root:
        raise EvidencePackError("evidence pack 验证期间根目录漂移")
    objects_parent = pack / "objects"
    objects = objects_parent / "sha256"
    _safe_directory(objects_parent, label="evidence objects", private=True)
    _safe_directory(objects, label="evidence sha256 objects", private=True)
    if {path.name for path in objects_parent.iterdir()} != {"sha256"}:
        raise EvidencePackError("evidence pack 验证期间 objects 布局漂移")
    expected_objects = {
        relative.rsplit("/", 1)[-1] for relative in identities
        if relative.startswith("objects/sha256/")}
    if {path.name for path in objects.iterdir()} != expected_objects:
        raise EvidencePackError("evidence pack 验证期间 object 集漂移")
    for relative, expected in identities.items():
        try:
            got = (_file_identity(root_info) if relative == "."
                   else _file_identity((pack / relative).lstat()))
        except OSError as error:
            raise EvidencePackError(
                f"evidence pack 验证期间文件丢失: {relative}") from error
        if got != expected:
            raise EvidencePackError(
                f"evidence pack 验证期间文件身份漂移: {relative}")


def _sync_dir(path: Path) -> None:
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, raw: bytes, *, mode: int) -> None:
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise EvidencePackError("evidence pack 写入无进展")
            offset += written
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _limits(max_files: int, max_bytes: int) -> tuple[int, int]:
    if (isinstance(max_files, bool) or not isinstance(max_files, int)
            or not 1 <= max_files <= 1_000_000):
        raise ValueError("max_files 须为 1..1000000")
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= 1024 * 1024 * 1024 * 1024):
        raise ValueError("max_bytes 须为 1..1TiB")
    return max_files, max_bytes


class _Builder:
    def __init__(self, root: Path, *, max_files: int, max_bytes: int):
        self.root = root
        self.objects = root / "objects" / "sha256"
        self.max_files, self.max_bytes = _limits(max_files, max_bytes)
        self.items: List[Dict[str, Any]] = []
        self.logical_ids: set[str] = set()
        self.digests: set[str] = set()
        self.total_bytes = 0

    def _new_item(self, *, kind: str, logical_id: str,
                  digest: str, size: int) -> str:
        logical_id = _logical_id(logical_id)
        if kind not in _ITEM_KINDS or logical_id in self.logical_ids:
            raise EvidencePackError(f"evidence item kind/id 冲突: {kind}/{logical_id}")
        if len(self.items) + 1 > self.max_files:
            raise EvidencePackError("evidence pack 文件数超过显式上限")
        self.logical_ids.add(logical_id)
        self.items.append({
            "kind": kind, "logical_id": logical_id,
            "sha256": digest, "bytes": size,
        })
        return logical_id

    def _commit_object(self, temporary: Path, *, digest: str, size: int) -> None:
        destination = self.objects / digest
        if digest in self.digests:
            temporary.unlink()
            return
        if self.total_bytes + size > self.max_bytes:
            temporary.unlink(missing_ok=True)
            raise EvidencePackError("evidence pack 总字节超过显式上限")
        os.rename(temporary, destination)
        _sync_dir(self.objects)
        self.digests.add(digest)
        self.total_bytes += size

    def add_bytes(self, *, kind: str, logical_id: str, raw: bytes) -> str:
        if len(raw) > _MAX_SINGLE_OBJECT_BYTES:
            raise EvidencePackError("evidence object 超过单对象上限")
        digest = _hash(raw)
        temporary = self.objects / f".{uuid.uuid4().hex}.tmp"
        _write_new(temporary, raw, mode=0o400)
        self._commit_object(temporary, digest=digest, size=len(raw))
        return self._new_item(
            kind=kind, logical_id=logical_id, digest=digest, size=len(raw))

    def add_json(self, *, kind: str, logical_id: str,
                 value: Mapping[str, Any]) -> str:
        return self.add_bytes(kind=kind, logical_id=logical_id,
                              raw=_canonical(dict(value)))

    def add_file(self, *, kind: str, logical_id: str, source: Path) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            source_fd = os.open(source, flags)
        except OSError as error:
            raise EvidencePackError(f"evidence source 不可打开: {source}") from error
        temporary = self.objects / f".{uuid.uuid4().hex}.tmp"
        destination_fd = -1
        try:
            before = os.fstat(source_fd)
            path_before = source.lstat()
            if (not stat.S_ISREG(before.st_mode)
                    or before.st_uid != os.geteuid() or before.st_mode & 0o022
                    or (before.st_dev, before.st_ino)
                    != (path_before.st_dev, path_before.st_ino)
                    or before.st_size < 0
                    or before.st_size > _MAX_SINGLE_OBJECT_BYTES):
                raise EvidencePackError(f"evidence source authority/大小非法: {source}")
            destination_fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o400)
            digest = hashlib.sha256()
            copied = 0
            while copied < before.st_size:
                block = os.read(source_fd, min(_COPY_BLOCK, before.st_size - copied))
                if not block:
                    raise EvidencePackError(f"evidence source 提前 EOF: {source}")
                digest.update(block)
                copied += len(block)
                offset = 0
                while offset < len(block):
                    written = os.write(destination_fd, block[offset:])
                    if written <= 0:
                        raise EvidencePackError("evidence object copy 无进展")
                    offset += written
            os.fchmod(destination_fd, 0o400)
            os.fsync(destination_fd)
            after = os.fstat(source_fd)
            path_after = source.lstat()
            identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            if (copied != before.st_size
                    or identity != (after.st_dev, after.st_ino,
                                    after.st_size, after.st_mtime_ns)
                    or (after.st_dev, after.st_ino)
                    != (path_after.st_dev, path_after.st_ino)):
                raise EvidencePackError(f"evidence source copy 期间漂移: {source}")
            value = digest.hexdigest()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            if destination_fd >= 0:
                os.close(destination_fd)
            os.close(source_fd)
        self._commit_object(temporary, digest=value, size=copied)
        return self._new_item(
            kind=kind, logical_id=logical_id, digest=value, size=copied)

    def add_tree(self, *, kind: str, logical_prefix: str, source: Path) -> List[str]:
        _safe_directory(source, label=f"{logical_prefix} tree")
        added = []
        for current, directories, files in os.walk(
                source, topdown=True, followlinks=False):
            current_path = Path(current)
            _safe_directory(current_path, label=f"{logical_prefix} directory")
            directories.sort()
            files.sort()
            for name in directories:
                _safe_directory(
                    current_path / name, label=f"{logical_prefix} child directory")
            for name in files:
                path = current_path / name
                rel = path.relative_to(source).as_posix()
                added.append(self.add_file(
                    kind=kind, logical_id=f"{logical_prefix}/{rel}", source=path))
        return added


def _cleanup_private_pack(root: Path) -> None:
    """Remove only the exact private layout created by this module."""
    if not os.path.lexists(root):
        return
    try:
        _safe_directory(root, label="temporary evidence pack", private=True)
        objects_parent = root / "objects"
        objects = objects_parent / "sha256"
        if os.path.lexists(objects):
            _safe_directory(objects, label="temporary evidence objects", private=True)
            for path in objects.iterdir():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise EvidencePackError("temporary evidence pack 含未知对象")
                path.unlink()
            objects.rmdir()
        if os.path.lexists(objects_parent):
            _safe_directory(
                objects_parent, label="temporary evidence objects parent", private=True)
            objects_parent.rmdir()
        for name in ("manifest.json", "READY.json"):
            (root / name).unlink(missing_ok=True)
        root.rmdir()
    except FileNotFoundError:
        pass


def _storage_items(builder: _Builder, archive: SnapshotArchive, *, prefix: str,
                   selected_cycles: Optional[Iterable[int]] = None,
                   verified_report: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    report = (dict(verified_report) if verified_report is not None
              else archive.verify())
    report_id = builder.add_json(
        kind="report", logical_id=f"{prefix}/storage/verify.json", value=report)
    high_water = _cycle(report["high_water_cycle"], label=f"{prefix} high-water")
    cycles = sorted(set(selected_cycles or [high_water]))
    if high_water not in cycles:
        cycles.append(high_water)
        cycles.sort()
    genesis_id = builder.add_file(
        kind="storage_genesis", logical_id=f"{prefix}/storage/genesis.json",
        source=archive.publisher.genesis_path)
    entries: Dict[int, Dict[str, Any]] = {}
    for cycle_id in cycles:
        pointer_path = archive.publisher.cycles / f"c{cycle_id}.json"
        pointer_raw = _read_regular(pointer_path, label=f"{prefix} pointer c{cycle_id}")
        pointer = _strict_json(pointer_raw, label=f"{prefix} pointer c{cycle_id}")
        manifest_path = archive.work_root / str(pointer.get("manifest_path"))
        manifest_raw = _read_regular(
            manifest_path, label=f"{prefix} manifest c{cycle_id}")
        if _hash(manifest_raw) != pointer.get("manifest_sha256"):
            raise EvidencePackError(f"{prefix} manifest c{cycle_id} hash 漂移")
        manifest = _strict_json(manifest_raw, label=f"{prefix} manifest c{cycle_id}")
        backup = manifest.get("backup")
        if not isinstance(backup, dict):
            raise EvidencePackError(f"{prefix} manifest c{cycle_id} backup 缺失")
        backup_path = archive.work_root / str(backup.get("path"))
        pointer_id = builder.add_bytes(
            kind="storage_pointer", logical_id=f"{prefix}/storage/c{cycle_id}.pointer.json",
            raw=pointer_raw)
        manifest_id = builder.add_bytes(
            kind="storage_manifest", logical_id=f"{prefix}/storage/c{cycle_id}.manifest.json",
            raw=manifest_raw)
        snapshot_id = builder.add_file(
            kind="sqlite_snapshot", logical_id=f"{prefix}/storage/c{cycle_id}.sqlite",
            source=backup_path)
        entries[cycle_id] = {
            "pointer": pointer, "manifest": manifest,
            "pointer_item": pointer_id, "manifest_item": manifest_id,
            "snapshot_item": snapshot_id,
        }
    return {
        "report": report, "report_item": report_id,
        "genesis_item": genesis_id, "high_water": high_water,
        "cycles": entries,
    }


def _log_items(builder: _Builder, archive: SnapshotArchive) -> Dict[str, Any]:
    assets = RegisteredAssetArchive(archive)
    report = assets.verify_log_mirrors()
    builder.add_json(
        kind="report", logical_id="source/log-mirrors/verify.json", value=report)
    indexes, objects = assets._scan_layout()  # exact closure already checked above
    referenced = set()
    for log_id, path in sorted(indexes.items()):
        value = _strict_json(
            _read_regular(path, label=f"log mirror index {log_id}"),
            label=f"log mirror index {log_id}")
        mirror = value.get("mirror")
        digest = mirror.get("sha256") if isinstance(mirror, dict) else None
        if not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None:
            raise EvidencePackError(f"log mirror index {log_id} digest 非法")
        builder.add_file(
            kind="log_mirror_index",
            logical_id=f"source/log-mirrors/indexes/execution-log-{log_id}.json",
            source=path)
        if digest not in referenced:
            builder.add_file(
                kind="log_mirror_object",
                logical_id=f"source/log-mirrors/objects/{digest}.gz",
                source=objects[digest])
            referenced.add(digest)
    return report


def _dependency_relative_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidencePackError(f"{label} 须为非空路径")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise EvidencePackError(f"{label} 非 UTF-8") from error
    parts = value.split("/")
    if (len(encoded) > 4096 or len(parts) > 128 or "\\" in value
            or PurePosixPath(value).is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
            or any(ord(character) < 0x20 or ord(character) == 0x7f
                   for character in value)):
        raise EvidencePackError(f"{label} 非安全相对路径")
    return PurePosixPath(value).as_posix()


def _dependency_semantic_paths(
        receipt: Mapping[str, Any], installed: Mapping[str, Any]) -> set[str]:
    """Project the provider receipt to the files needed for offline restore proof."""
    lock = receipt.get("lock")
    wheels = receipt.get("wheels")
    files = installed.get("files")
    if (not isinstance(lock, Mapping) or not isinstance(wheels, list)
            or not isinstance(files, list)):
        raise EvidencePackError("dependency receipt/manifest 无法投影文件闭包")
    lock_source = _dependency_relative_path(
        lock.get("path"), label="dependency lock source path")
    lock_name = PurePosixPath(lock_source).name
    if lock_name != "python-wheel-lock.json":
        raise EvidencePackError("dependency lock basename 非法")
    result = {
        "receipt.json", "installed-manifest.json", lock_name,
        "runtime/runtime.json", "runtime/runtime.log",
        "runtime/runtime.log.exit", "check/pip-check.log",
        "check/pip-check.log.exit", "context/Dockerfile", "image.tar",
    }
    for wheel in wheels:
        filename = wheel.get("filename") if isinstance(wheel, Mapping) else None
        relative = _dependency_relative_path(
            filename, label="dependency wheel filename")
        if PurePosixPath(relative).name != relative:
            raise EvidencePackError("dependency wheel filename 非 basename")
        result.add("wheelhouse/" + relative)
    for item in files:
        path = item.get("path") if isinstance(item, Mapping) else None
        relative = _dependency_relative_path(
            path, label="dependency installed path")
        result.add("install/site-packages/" + relative)
        result.add("context/site-packages/" + relative)
    return result


def _import_items(builder: _Builder, archive: SnapshotArchive) -> Dict[str, Any]:
    imports = ImportMaterializationArchive(archive)
    report = imports.verify()
    builder.add_json(
        kind="report", logical_id="source/import-materializations/verify.json",
        value=report)
    for obj in report["repository_objects"]:
        digest = obj["object_hash"].removeprefix("sha256:")
        builder.add_tree(
            kind="import_repository_file",
            logical_prefix=f"source/import-materializations/objects/{digest}",
            source=imports.repository_objects / digest)
        for name in obj["indexes"]:
            builder.add_file(
                kind="import_index",
                logical_id=f"source/import-materializations/indexes/{name}",
                source=imports.repository_indexes / name)
    for value in report["dependency_objects"]:
        digest = value.removeprefix("sha256:")
        source = imports.dependency_objects / digest
        receipt = _strict_json(
            _read_regular(source / "receipt.json", label="dependency receipt"),
            label="dependency receipt")
        installed = _strict_json(
            _read_regular(
                source / "installed-manifest.json",
                label="dependency installed manifest"),
            label="dependency installed manifest")
        if not isinstance(receipt, dict) or not isinstance(installed, dict):
            raise EvidencePackError("dependency receipt/manifest 非 object")
        prefix = (
            f"source/import-materializations/dependency-images/objects/{digest}")
        for relative in sorted(_dependency_semantic_paths(receipt, installed)):
            builder.add_file(
                kind="dependency_image_file",
                logical_id=f"{prefix}/{relative}", source=source / relative)
    return report


def _fault_items(builder: _Builder, work_root: Path) -> None:
    root = work_root / "state" / "fault-schedules"
    if not os.path.lexists(root):
        return
    _safe_directory(root, label="fault schedule root")
    for schedule_root in sorted(root.iterdir(), key=lambda path: path.name):
        if _SCHEDULE_RE.fullmatch(schedule_root.name) is None:
            raise EvidencePackError("fault schedule root 含非法 schedule_id")
        _safe_directory(schedule_root, label="fault schedule state")
        schedule_path = schedule_root / "schedule.json"
        final = verify_fault_schedule(schedule_path)
        if (final.get("status") != "complete"
                or final.get("signal_exactly_once") is not False
                or final.get("recovery_verified") is not False):
            raise EvidencePackError("只打包 complete 且诚实边界未改写的 fault schedule")
        builder.add_json(
            kind="report",
            logical_id=f"source/fault-schedules/{schedule_root.name}/verify.json",
            value=final)
        builder.add_tree(
            kind="fault_receipt",
            logical_prefix=f"source/fault-schedules/{schedule_root.name}/state",
            source=schedule_root)


def _qualification_items(
        builder: _Builder, work_root: Path, *, resume_requested: bool) -> None:
    root = work_root / "state" / "qualification"
    contract = root / "contract.json"
    if not os.path.lexists(root):
        return
    _safe_directory(root, label="qualification receipt root")
    if not os.path.lexists(contract):
        raise EvidencePackError("qualification state 存在但 contract 缺失")
    if resume_requested:
        raise EvidencePackError(
            "qualification work-root 当前无安全 restore closure；拒绝伪装普通 resume")
    firewall = load_qualification_firewall(work_root, require_research_uid=True)
    if firewall is None:
        raise EvidencePackError("qualification contract 未能加载")
    builder.add_tree(
        kind="qualification_receipt", logical_prefix="source/qualification/state",
        source=root)
    report = {
        "status": "receipt_only", "task": firewall.task,
        "contract_sha256": _hash(_read_regular(
            contract, label="qualification contract")),
        "qualification_complete_claimed": False,
    }
    builder.add_json(
        kind="report", logical_id="source/qualification/verify.json", value=report)


def _canary_items(
        builder: _Builder, *, canary_root: Optional[Path],
        canary_run_id: Optional[str], canary_scope: str) -> None:
    if canary_root is None and canary_run_id is None:
        return
    if canary_root is None or canary_run_id is None:
        raise ValueError("canary_root 与 canary_run_id 须同时提供")
    result = verify_canary(
        canary_root=canary_root, run_id=canary_run_id,
        required_scope=canary_scope)
    if result.get("status") != "passed":
        raise EvidencePackError("shared-fs canary 未通过")
    builder.add_json(
        kind="report", logical_id="shared-fs-canary/verify.json", value=result)
    for rel in (Path("state/shared-fs-canary"), Path("state/executions")):
        path = canary_root / rel
        if os.path.lexists(path):
            builder.add_tree(
                kind="canary_receipt",
                logical_prefix=f"shared-fs-canary/{rel.as_posix()}", source=path)
    database_path = canary_root / "research.sqlite"
    if os.path.lexists(database_path):
        builder.add_file(
            kind="sqlite_snapshot", logical_id="shared-fs-canary/research.sqlite",
            source=database_path)


def _manifest_with_hash(manifest: Mapping[str, Any]) -> tuple[bytes, str]:
    raw = _canonical(dict(manifest))
    if len(raw) > _MAX_JSON_BYTES:
        raise EvidencePackError("evidence manifest 超过上限")
    return raw, _hash(raw)


def _validate_resume(
        *, source_root: Path, target_root: Path,
        source: Mapping[str, Any], target_archive: SnapshotArchive,
        builder: _Builder) -> Dict[str, Any]:
    source_cycle = int(source["high_water"])
    source_entry = source["cycles"][source_cycle]
    source_manifest = source_entry["manifest"]
    source_manifest_hash = source_entry["pointer"]["manifest_sha256"]
    restore_path = target_root / "restore.json"
    restore_raw = _read_regular(restore_path, label="resume restore receipt")
    restore = _strict_json(restore_raw, label="resume restore receipt")
    expected_restore_fields = {
        "schema", "scope", "continuation_mode", "publication_contract",
        "source_work_root", "source_cycle", "source_manifest_sha256", "backup",
    }
    if (set(restore) != expected_restore_fields
            or restore.get("schema") != "meta-research-storage-restore/v1"
            or restore.get("scope") != "sqlite_truth_only"
            or restore.get("source_work_root") != str(source_root)
            or restore.get("source_cycle") != f"c{source_cycle}"
            or restore.get("source_manifest_sha256") != source_manifest_hash
            or restore.get("backup") != source_manifest.get("backup")):
        raise EvidencePackError("resume restore receipt 未绑定 exact source snapshot")
    if (os.path.lexists(target_root / RESTORE_IN_PROGRESS_NAME)
            or os.path.lexists(target_root.parent / restore_parent_claim_name(target_root))):
        raise EvidencePackError("resume target 仍有 restore marker/parent claim")
    if os.path.lexists(source_root / "state" / "qualification" / "contract.json"):
        raise EvidencePackError("qualification source 不得降格为普通 resume probe")
    if os.path.lexists(target_root / "state" / "qualification" / "contract.json"):
        raise EvidencePackError("resume target qualification 身份意外出现")

    report = target_archive.verify()
    coverage = _cycle(report["coverage_start_cycle"], label="resume coverage")
    high_water = _cycle(report["high_water_cycle"], label="resume high-water")
    # v1 is intentionally exact-one-cycle.  This makes the provenance
    # predicate simple and prevents an unrelated later run from hiding the
    # first post-restore outcome.
    if coverage != source_cycle or high_water != source_cycle + 1:
        raise EvidencePackError(
            "resume probe 须从 source adoption 后精确新增一轮")
    target = _storage_items(
        builder, target_archive, prefix="resume",
        selected_cycles=[source_cycle, source_cycle + 1],
        verified_report=report)
    genesis_raw = _read_regular(
        target_archive.publisher.genesis_path, label="resume storage genesis")
    genesis = _strict_json(genesis_raw, label="resume storage genesis")
    adoption = target["cycles"][source_cycle]
    post = target["cycles"][source_cycle + 1]
    if (genesis.get("adoption_baseline") is not True
            or genesis.get("coverage_start_cycle") != source_cycle
            or genesis.get("bootstrap_before_cycle") != source_cycle - 1
            or adoption["manifest"].get("adoption_baseline") is not True
            or adoption["manifest"].get("bootstrap_before_cycle") != source_cycle - 1
            or adoption["manifest"].get("cycle_status")
            != source_manifest.get("cycle_status")
            or adoption["manifest"].get("backup") != source_manifest.get("backup")
            or adoption["manifest"].get("previous_manifest_sha256") is not None
            or post["manifest"].get("adoption_baseline") is not False
            or post["manifest"].get("bootstrap_before_cycle") is not None
            or post["manifest"].get("previous_manifest_sha256")
            != adoption["pointer"].get("manifest_sha256")
            or post["manifest"].get("cycle_status") != "done"):
        raise EvidencePackError("resume adoption/post-cycle chain 不满足单轮续跑合同")

    post_snapshot = target_root / post["manifest"]["backup"]["path"]
    fd = os.open(
        post_snapshot, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0))
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(
            f"file:{quote(f'/proc/self/fd/{fd}')}?mode=ro&immutable=1", uri=True)
        row = connection.execute(
            "SELECT status,route FROM cycle WHERE id=?", (source_cycle + 1,)).fetchone()
        if row is None or row[0] != "done" or not isinstance(row[1], str) or not row[1]:
            raise EvidencePackError("resume 新轮次不是有 route 的真实 done cycle")
        runner_call_ids = [row[0] for row in connection.execute(
            "SELECT DISTINCT rc.id FROM runner_call rc JOIN ledger l "
            "ON l.runner_call_id=rc.id AND l.cycle_id=rc.cycle_id "
            "WHERE rc.cycle_id=? AND rc.status='success' "
            "AND rc.phase IN ('idea','plan','bundle','reasoning') ORDER BY rc.id",
            (source_cycle + 1,))]
        if not runner_call_ids:
            raise EvidencePackError("resume 新轮次缺 success runner_call + ledger")
    except sqlite3.Error as error:
        raise EvidencePackError("resume post snapshot 无法核验 cycle") from error
    finally:
        if connection is not None:
            connection.close()
        os.close(fd)

    builder.add_bytes(
        kind="restore_receipt", logical_id="resume/restore.json", raw=restore_raw)
    if restore.get("continuation_mode") == "import_materialization_restore_required":
        completion = target_root / "state" / "import-materializations" / "storage-restore.json"
        completion_raw = _read_regular(
            completion, label="resume import materialization completion")
        _strict_json(completion_raw, label="resume import materialization completion")
        builder.add_bytes(
            kind="restore_receipt",
            logical_id="resume/import-materializations/storage-restore.json",
            raw=completion_raw)
    elif restore.get("continuation_mode") != "legacy_adoption_on_first_start":
        raise EvidencePackError("resume continuation_mode 非法")
    return {"protocol": RESUME_PROTOCOL}


def create_evidence_pack(
        *, source_work_root: Path | str, output_parent: Path | str,
        resume_work_root: Optional[Path | str] = None,
        canary_root: Optional[Path | str] = None,
        canary_run_id: Optional[str] = None,
        canary_scope: str = LOCAL_SCOPE,
        max_files: int = DEFAULT_MAX_FILES,
        max_bytes: int = DEFAULT_MAX_BYTES) -> Dict[str, Any]:
    """Freeze one coherent source high-water and an optional resume probe."""
    max_files, max_bytes = _limits(max_files, max_bytes)
    if canary_scope not in {LOCAL_SCOPE, TWO_NODE_SCOPE}:
        raise ValueError("canary_scope 非法")
    source_root = Path(os.path.abspath(os.fspath(source_work_root)))
    output = Path(os.path.abspath(os.fspath(output_parent)))
    resume_root = (
        None if resume_work_root is None
        else Path(os.path.abspath(os.fspath(resume_work_root))))
    canary = (
        None if canary_root is None
        else Path(os.path.abspath(os.fspath(canary_root))))
    _canonical_directory(source_root, label="source work-root")
    _canonical_directory(output, label="evidence output parent")
    if resume_root is not None:
        _canonical_directory(resume_root, label="resume work-root")
    if canary is not None:
        _canonical_directory(canary, label="shared-fs canary root")
    if (resume_root is not None
            and (resume_root == source_root
                 or source_root in resume_root.parents
                 or resume_root in source_root.parents)):
        raise EvidencePackError("source/resume work-root 必须互不嵌套")
    protected_roots = [source_root, *([resume_root] if resume_root is not None else []),
                       *([canary] if canary is not None else [])]
    if any(output == root or root in output.parents for root in protected_roots):
        raise EvidencePackError("evidence output parent 不得位于被取证 root 内")
    temporary = output / f".evidence-pack-{uuid.uuid4().hex}"
    source_lease: Optional[InstanceLease] = None
    resume_lease: Optional[InstanceLease] = None
    primary: Optional[BaseException] = None
    try:
        temporary.mkdir(mode=0o700)
        (temporary / "objects").mkdir(mode=0o700)
        (temporary / "objects" / "sha256").mkdir(mode=0o700)
        builder = _Builder(temporary, max_files=max_files, max_bytes=max_bytes)
        source_lease = InstanceLease.acquire(source_root)
        source_archive = SnapshotArchive(work_root=source_root, lease=source_lease)
        source = _storage_items(builder, source_archive, prefix="source")
        log_report = _log_items(builder, source_archive)
        import_report = _import_items(builder, source_archive)
        for report, label in (
                (log_report, "log mirror"), (import_report, "import materialization")):
            if (report.get("high_water_cycle") != source["report"]["high_water_cycle"]
                    or report.get("high_water_manifest_sha256")
                    != source["report"]["high_water_manifest_sha256"]):
                raise EvidencePackError(f"{label} report 与 source high-water 不一致")
        _fault_items(builder, source_root)
        _qualification_items(
            builder, source_root, resume_requested=resume_root is not None)
        _canary_items(
            builder, canary_root=canary, canary_run_id=canary_run_id,
            canary_scope=canary_scope)

        resume_probe = None
        if resume_root is not None:
            resume_lease = InstanceLease.acquire(resume_root)
            resume_archive = SnapshotArchive(
                work_root=resume_root, lease=resume_lease)
            resume_probe = _validate_resume(
                source_root=source_root, target_root=resume_root,
                source=source, target_archive=resume_archive, builder=builder)
        builder.items.sort(key=lambda value: value["logical_id"])
        manifest = {
            "version": 1, "protocol": PACK_PROTOCOL,
            "source_work_root": str(source_root),
            "resume_probe": resume_probe,
            "items": builder.items,
        }
        manifest_raw, manifest_hash = _manifest_with_hash(manifest)
        ready = {
            "version": 1, "protocol": READY_PROTOCOL,
            "manifest_sha256": manifest_hash,
        }
        ready_raw = _canonical(ready)
        if (len(builder.digests) + 2 > max_files
                or builder.total_bytes + len(manifest_raw) + len(ready_raw) > max_bytes):
            raise EvidencePackError("evidence pack 物理文件/总字节超过显式上限")
        _write_new(temporary / "manifest.json", manifest_raw, mode=0o400)
        _write_new(temporary / "READY.json", ready_raw, mode=0o400)
        _sync_dir(temporary / "objects" / "sha256")
        _sync_dir(temporary / "objects")
        _sync_dir(temporary)
        destination = output / f"{manifest_hash}{PACK_SUFFIX}"
        reused = False
        if os.path.lexists(destination):
            result = verify_evidence_pack(destination)
            if result["manifest_sha256"] != manifest_hash:
                raise EvidencePackError("同 pack-id 已存在冲突内容")
            _cleanup_private_pack(temporary)
            reused = True
        else:
            # Run the same offline verifier before the content-addressed final
            # name can become visible.  A semantic failure therefore leaves only
            # private staging, which the finally block removes.  An already
            # verified identical destination needs no redundant staging scan.
            staged_result = verify_evidence_pack(
                temporary, _expected_manifest_sha256=manifest_hash)
            staged_identities = staged_result.pop("_verified_file_identities")
            try:
                # A valid destination is non-empty, so POSIX rename cannot replace
                # it.  This remains atomic on filesystems without renameat2 flags.
                os.rename(temporary, destination)
                _sync_dir(output)
            except OSError as error:
                if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                result = verify_evidence_pack(destination)
                if result["manifest_sha256"] != manifest_hash:
                    raise EvidencePackError("同 pack-id 已存在冲突内容") from error
                _cleanup_private_pack(temporary)
                reused = True
            else:
                # Renaming the staging root intentionally changes that
                # directory's ctime; all nested authority must stay identical.
                staged_identities.pop(".")
                try:
                    _assert_pack_unchanged(destination, staged_identities)
                except BaseException as verification_error:
                    try:
                        _cleanup_private_pack(destination)
                        _sync_dir(output)
                    except BaseException as cleanup_error:
                        add_note = getattr(verification_error, "add_note", None)
                        if callable(add_note):
                            add_note(
                                "post-publish evidence cleanup 失败: "
                                f"{cleanup_error}")
                    raise
                result = staged_result
        result["pack_path"] = str(destination)
        result["reused"] = reused
        return result
    except BaseException as error:
        primary = error
        raise
    finally:
        close_errors = []
        for lease in (resume_lease, source_lease):
            if lease is not None:
                error = lease.close()
                if error is not None:
                    close_errors.append(error)
        if os.path.lexists(temporary):
            try:
                _cleanup_private_pack(temporary)
            except BaseException as error:
                close_errors.append(error)
        if close_errors:
            if primary is not None:
                add_note = getattr(primary, "add_note", None)
                if callable(add_note):
                    for error in close_errors:
                        add_note(f"evidence cleanup 失败: {error}")
            else:
                raise close_errors[0]


def _item_map(manifest: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise EvidencePackError("evidence items 须为非空数组")
    result = {}
    previous = None
    for item in items:
        if (not isinstance(item, dict)
                or set(item) != {"kind", "logical_id", "sha256", "bytes"}
                or item.get("kind") not in _ITEM_KINDS
                or not isinstance(item.get("sha256"), str)
                or _HASH_RE.fullmatch(item["sha256"]) is None
                or isinstance(item.get("bytes"), bool)
                or not isinstance(item.get("bytes"), int)
                or not 0 <= item["bytes"] <= _MAX_SINGLE_OBJECT_BYTES):
            raise EvidencePackError("evidence item 字段非法")
        logical_id = _logical_id(item.get("logical_id"))
        if (previous is not None and logical_id <= previous) or logical_id in result:
            raise EvidencePackError("evidence items 未严格排序/重复")
        previous = logical_id
        result[logical_id] = item
    return result


def _object_bytes(pack: Path, item: Mapping[str, Any]) -> bytes:
    if item.get("kind") not in _JSON_ITEM_KINDS or item.get("bytes", 0) > _MAX_JSON_BYTES:
        raise EvidencePackError(
            f"evidence item 不是有界 JSON object: {item.get('logical_id')}")
    return _read_regular(
        pack / "objects" / "sha256" / item["sha256"],
        label=f"evidence object {item['logical_id']}",
        maximum=min(_MAX_JSON_BYTES, item["bytes"] + 1))


def _object_path(pack: Path, item: Mapping[str, Any]) -> Path:
    return pack / "objects" / "sha256" / str(item["sha256"])


def _packed_json_value(
        pack: Path, item: Mapping[str, Any], *, label: str) -> Any:
    size = item.get("bytes")
    if (isinstance(size, bool) or not isinstance(size, int)
            or not 2 <= size <= _MAX_JSON_BYTES):
        raise EvidencePackError(f"{label} JSON 大小非法")
    raw = _read_regular(
        _object_path(pack, item), label=label, maximum=size)
    if len(raw) != size:
        raise EvidencePackError(f"{label} bytes 声明漂移")
    return _strict_json_value(raw, label=label, maximum=size)


def _validate_storage_object(
        pack: Path, items: Mapping[str, Mapping[str, Any]], *, prefix: str,
        cycle_id: int, allow_later_cycles: bool) -> Dict[str, Any]:
    pointer_item = items.get(f"{prefix}/storage/c{cycle_id}.pointer.json")
    manifest_item = items.get(f"{prefix}/storage/c{cycle_id}.manifest.json")
    snapshot_item = items.get(f"{prefix}/storage/c{cycle_id}.sqlite")
    if (pointer_item is None or pointer_item.get("kind") != "storage_pointer"
            or manifest_item is None
            or manifest_item.get("kind") != "storage_manifest"
            or snapshot_item is None
            or snapshot_item.get("kind") != "sqlite_snapshot"):
        raise EvidencePackError(f"{prefix} c{cycle_id} storage item 缺失")
    pointer_raw = _object_bytes(pack, pointer_item)
    manifest_raw = _object_bytes(pack, manifest_item)
    pointer = _strict_json(pointer_raw, label=f"{prefix} pointer")
    manifest = _strict_json(manifest_raw, label=f"{prefix} manifest")
    digest = _hash(manifest_raw)
    backup = manifest.get("backup")
    views = manifest.get("views")
    if (set(pointer) != {"schema", "cycle_id", "manifest_sha256", "manifest_path"}
            or pointer.get("schema") != sg.SCHEMA
            or pointer.get("cycle_id") != f"c{cycle_id}"
            or pointer.get("manifest_sha256") != digest
            or pointer.get("manifest_path")
            != f"state/storage/manifests/sha256/{digest}.json"
            or manifest.get("schema") != sg.SCHEMA
            or manifest.get("cycle_id") != f"c{cycle_id}"
            or set(manifest) != {
                "schema", "cycle_id", "cycle_status", "bootstrap_before_cycle",
                "adoption_baseline", "previous_manifest_sha256", "backup", "views",
                "asset_inventory_sha256", "assets"}
            or not isinstance(backup, dict)
            or set(backup) != {"path", "sha256", "bytes", "schema_version"}
            or backup.get("sha256") != snapshot_item["sha256"]
            or backup.get("bytes") != snapshot_item["bytes"]
            or backup.get("schema_version") != database.SCHEMA_VERSION
            or backup.get("path")
            != f"state/storage/backups/sha256/{snapshot_item['sha256']}.sqlite"
            or manifest.get("cycle_status") not in sg.TERMINAL_CYCLE_STATES
            or not isinstance(manifest.get("adoption_baseline"), bool)
            or not isinstance(views, dict) or set(views) != {"path", "commit", "tree"}
            or views.get("path") != "views"
            or not isinstance(views.get("commit"), str)
            or re.fullmatch(r"[0-9a-f]{40,64}", views["commit"]) is None
            or not isinstance(views.get("tree"), str)
            or re.fullmatch(r"[0-9a-f]{40,64}", views["tree"]) is None
            or not isinstance(manifest.get("assets"), list)
            or _hash(sg._canonical(manifest["assets"]))
            != manifest.get("asset_inventory_sha256")):
        raise EvidencePackError(f"{prefix} c{cycle_id} pointer/manifest 漂移")
    snapshot_path = _object_path(pack, snapshot_item)
    # Reuse the production deep SQLite verifier.  The method has no instance
    # state dependency and anchors both hash and SQLite to one O_NOFOLLOW fd.
    sg.CycleSnapshotPublisher._verify_backup_object(  # noqa: SLF001
        object(), snapshot_path,
        expected_hash=snapshot_item["sha256"], expected_bytes=snapshot_item["bytes"],
        cycle_id=cycle_id, cycle_status=manifest["cycle_status"],
        allow_later_cycles=allow_later_cycles)
    return {"pointer": pointer, "manifest": manifest}


def _validate_resume_offline(
        pack: Path, probe: Any, items: Mapping[str, Mapping[str, Any]], *,
        source_cycle: int, source_work_root: str,
        source_storage: Mapping[str, Any]) -> bool:
    """Derive the optional exact-one-cycle claim from packed facts only."""
    resume_ids = {
        logical_id for logical_id in items if logical_id.startswith("resume/")}
    if probe is None:
        if resume_ids:
            raise EvidencePackError("resume probe 缺失却含 resume evidence")
        return False
    if (not isinstance(probe, dict)
            or probe != {"protocol": RESUME_PROTOCOL}):
        raise EvidencePackError("resume probe 字段非法")

    post_cycle = source_cycle + 1
    source_cycle_name = f"c{source_cycle}"
    post_cycle_name = f"c{post_cycle}"
    adoption = _validate_storage_object(
        pack, items, prefix="resume", cycle_id=source_cycle,
        allow_later_cycles=True)
    post = _validate_storage_object(
        pack, items, prefix="resume", cycle_id=post_cycle,
        allow_later_cycles=False)
    resume_report = _packed_json(
        pack, items, "resume/storage/verify.json", kind="report",
        label="packed resume storage report")
    resume_genesis = _packed_json(
        pack, items, "resume/storage/genesis.json", kind="storage_genesis",
        label="packed resume storage genesis")
    if (adoption["manifest"].get("adoption_baseline") is not True
            or adoption["manifest"].get("bootstrap_before_cycle")
            != source_cycle - 1
            or adoption["manifest"].get("cycle_status")
            != source_storage["manifest"].get("cycle_status")
            or adoption["manifest"].get("backup")
            != source_storage["manifest"].get("backup")
            or adoption["manifest"].get("previous_manifest_sha256") is not None
            or post["manifest"].get("previous_manifest_sha256")
            != adoption["pointer"]["manifest_sha256"]
            or post["manifest"].get("adoption_baseline") is not False
            or post["manifest"].get("bootstrap_before_cycle") is not None
            or post["manifest"].get("cycle_status") != "done"
            or resume_report.get("schema") != "meta-research-storage-verify/v1"
            or resume_report.get("scope") != "snapshot_chain_and_retained_sqlite"
            or resume_report.get("coverage_start_cycle") != source_cycle_name
            or resume_report.get("high_water_cycle") != post_cycle_name
            or resume_report.get("high_water_manifest_sha256")
            != post["pointer"]["manifest_sha256"]
            or resume_report.get("views_commit")
            != post["manifest"].get("views", {}).get("commit")
            or not isinstance(resume_report.get("deep_verified_cycles"), list)
            or post_cycle_name not in resume_report["deep_verified_cycles"]
            or set(resume_genesis) != {
                "schema", "coverage_start_cycle", "adoption_baseline",
                "bootstrap_before_cycle"}
            or resume_genesis.get("schema") != sg.GENESIS_SCHEMA
            or resume_genesis.get("coverage_start_cycle") != source_cycle
            or resume_genesis.get("adoption_baseline") is not True
            or resume_genesis.get("bootstrap_before_cycle") != source_cycle - 1):
        raise EvidencePackError("resume probe storage 绑定漂移")

    restore = _packed_json(
        pack, items, "resume/restore.json", kind="restore_receipt",
        label="packed restore receipt")
    if (set(restore) != {
            "schema", "scope", "continuation_mode", "publication_contract",
            "source_work_root", "source_cycle", "source_manifest_sha256", "backup"}
            or restore.get("schema") != "meta-research-storage-restore/v1"
            or restore.get("scope") != "sqlite_truth_only"
            or restore.get("publication_contract")
            != "atomic_noreplace_or_lease_fenced_ready"
            or restore.get("source_work_root") != source_work_root
            or restore.get("source_cycle") != source_cycle_name
            or restore.get("source_manifest_sha256")
            != source_storage["pointer"]["manifest_sha256"]
            or restore.get("backup") != source_storage["manifest"]["backup"]):
        raise EvidencePackError("packed restore receipt 与 probe 漂移")

    completion_id = "resume/import-materializations/storage-restore.json"
    completion_item = items.get(completion_id)
    if restore.get("continuation_mode") == "import_materialization_restore_required":
        if completion_item is None or completion_item.get("kind") != "restore_receipt":
            raise EvidencePackError("resume import completion receipt 缺失")
        completion = _strict_json(
            _object_bytes(pack, completion_item),
            label="packed import restore completion")
        import_report = _packed_json(
            pack, items, "source/import-materializations/verify.json",
            kind="report", label="packed import materialization report")
        expected_repositories = [
            value["object_hash"] for value in import_report["repository_objects"]]
        expected_dependencies = list(import_report["dependency_objects"])
        expected_indexes = []
        for value in import_report["repository_objects"]:
            for name in value["indexes"]:
                index_item = items.get(
                    f"source/import-materializations/indexes/{name}")
                if index_item is None or index_item.get("kind") != "import_index":
                    raise EvidencePackError(
                        "packed import completion 对应 source index 缺失")
                expected_indexes.append({
                    "name": name,
                    "sha256": "sha256:" + index_item["sha256"],
                })
        expected_indexes.sort(key=lambda value: value["name"])
        if (set(completion) != {
                "schema", "scope", "source_work_root", "source_cycle",
                "source_manifest_sha256", "repository_objects",
                "repository_indexes", "dependency_objects"}
                or completion.get("schema")
                != "meta-research-import-materialization-restore/v1"
                or completion.get("scope")
                != "repository_and_dependency_cas_only"
                or completion.get("source_work_root") != source_work_root
                or completion.get("source_cycle") != source_cycle_name
                or completion.get("source_manifest_sha256")
                != source_storage["pointer"]["manifest_sha256"]
                or not isinstance(completion.get("repository_objects"), list)
                or not isinstance(completion.get("repository_indexes"), list)
                or not isinstance(completion.get("dependency_objects"), list)
                or completion["repository_objects"] != expected_repositories
                or completion["repository_indexes"] != expected_indexes
                or completion["dependency_objects"] != expected_dependencies):
            raise EvidencePackError("packed import restore completion 绑定漂移")
    elif (restore.get("continuation_mode") != "legacy_adoption_on_first_start"
          or completion_item is not None):
        raise EvidencePackError("legacy restore mode/import completion 漂移")

    expected_resume_ids = {
        "resume/storage/verify.json", "resume/storage/genesis.json",
        f"resume/storage/c{source_cycle}.pointer.json",
        f"resume/storage/c{source_cycle}.manifest.json",
        f"resume/storage/c{source_cycle}.sqlite",
        f"resume/storage/c{post_cycle}.pointer.json",
        f"resume/storage/c{post_cycle}.manifest.json",
        f"resume/storage/c{post_cycle}.sqlite",
        "resume/restore.json",
    }
    if completion_item is not None:
        expected_resume_ids.add(completion_id)
    if resume_ids != expected_resume_ids:
        raise EvidencePackError("resume evidence 逻辑闭包漂移")

    snapshot_item = items[f"resume/storage/c{post_cycle}.sqlite"]
    fd = os.open(
        _object_path(pack, snapshot_item),
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0))
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(
            f"file:{quote(f'/proc/self/fd/{fd}')}?mode=ro&immutable=1", uri=True)
        row = connection.execute(
            "SELECT status,route FROM cycle WHERE id=?", (post_cycle,)).fetchone()
        if row is None or row[0] != "done" or not isinstance(row[1], str) or not row[1]:
            raise EvidencePackError("packed resume cycle 不是有 route 的 done")
        runner_call = connection.execute(
            "SELECT rc.id FROM runner_call rc JOIN ledger l "
            "ON l.runner_call_id=rc.id AND l.cycle_id=rc.cycle_id "
            "WHERE rc.cycle_id=? AND rc.status='success' "
            "AND rc.phase IN ('idea','plan','bundle','reasoning') LIMIT 1",
            (post_cycle,)).fetchone()
        if runner_call is None:
            raise EvidencePackError(
                "packed resume cycle 缺 success runner_call + ledger")
    except sqlite3.Error as error:
        raise EvidencePackError("packed resume cycle 无法读取") from error
    finally:
        if connection is not None:
            connection.close()
        os.close(fd)
    return True


def _validate_log_mirrors_offline(
        pack: Path, items: Mapping[str, Mapping[str, Any]], *, source_cycle: int,
        source_work_root: str, source_assets: Sequence[Mapping[str, Any]],
        import_report: Mapping[str, Any],
        ) -> tuple[int, Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    snapshot_item = items[f"source/storage/c{source_cycle}.sqlite"]
    fd = os.open(
        _object_path(pack, snapshot_item),
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    connection: Optional[sqlite3.Connection] = None
    registered: Dict[int, Dict[str, Any]] = {}
    try:
        connection = sqlite3.connect(
            f"file:{quote(f'/proc/self/fd/{fd}')}?mode=ro&immutable=1", uri=True)
        database_assets: List[Dict[str, Any]] = []
        for row in connection.execute(
                "SELECT id,path,hash_alg,content_hash,artifact_type,origin,manifest_hash "
                "FROM checkpoint ORDER BY id"):
            database_assets.append({
                "owner": "checkpoint", "owner_id": int(row[0]), "ref": row[1],
                "hash_alg": row[2], "content_hash": row[3],
                "artifact_type": row[4], "origin": row[5],
                "manifest_hash": row[6], "retention": "registered_forever",
            })
        for row in connection.execute(
                "SELECT id,run_id,evaluation_attempt_id,cycle_id,log_kind,ref,"
                "content_hash,bytes FROM execution_log ORDER BY id"):
            digest = str(row[6]).removeprefix("sha256:")
            if (_HASH_RE.fullmatch(digest) is None
                    or isinstance(row[7], bool) or not isinstance(row[7], int)
                    or row[7] < 0):
                raise EvidencePackError("packed source execution_log identity 非法")
            ref_path = Path(row[5])
            try:
                normalized = Path(os.path.abspath(os.fspath(
                    ref_path if ref_path.is_absolute()
                    else Path(source_work_root) / ref_path)))
                relative_path = normalized.relative_to(
                    Path(source_work_root)).as_posix()
            except ValueError as error:
                raise EvidencePackError(
                    "packed source execution_log ref 越出 source") from error
            registered[row[0]] = {
                "id": row[0], "run_id": row[1], "evaluation_attempt_id": row[2],
                "cycle_id": f"c{row[3]}", "log_kind": row[4], "ref": row[5],
                "content_hash": row[6], "sha256": digest, "bytes": row[7],
                "relative_path": relative_path,
            }
            database_assets.append({
                "owner": "execution_log", "owner_id": int(row[0]),
                "ref": row[5], "hash_alg": "sha256", "content_hash": row[6],
                "bytes": row[7], "log_kind": row[4], "cycle_id": f"c{row[3]}",
                "retention": "registered_forever",
            })
        for row in connection.execute(
                "SELECT id,manifest_hash,action_cycle FROM external_import "
                "WHERE action='imported' ORDER BY id"):
            database_assets.append({
                "owner": "external_import", "owner_id": int(row[0]),
                "provenance_manifest_hash": row[1], "cycle_id": f"c{row[2]}",
                "retention": "db_provenance_only",
            })
        if database_assets != list(source_assets):
            raise EvidencePackError(
                "packed storage asset inventory 与 source SQLite 漂移")

        repository_roots: Dict[str, Dict[str, Any]] = {}
        legacy_targets: List[int] = []
        unbound_targets: List[int] = []
        import_target_count = 0

        def unique(pairs):  # noqa: ANN001
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError(f"duplicate key: {key!r}")
                value[key] = item
            return value

        for target_id, raw_ref in connection.execute(
                "SELECT id,plan_ref FROM build_target "
                "WHERE target_kind='import' ORDER BY id"):
            import_target_count += 1
            if raw_ref is None:
                unbound_targets.append(target_id)
                continue
            try:
                plan_ref = json.loads(
                    raw_ref, object_pairs_hook=unique,
                    parse_constant=lambda token: (_ for _ in ()).throw(
                        ValueError(f"non-finite {token}")))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise EvidencePackError(
                    f"packed source import target {target_id} plan_ref 非法") from error
            contract = plan_ref.get("materialization_contract") if isinstance(
                plan_ref, dict) else None
            if (isinstance(plan_ref, dict)
                    and set(plan_ref) == {"materialization_contract", "files"}
                    and isinstance(contract, dict)
                    and "repository_snapshot_hash" not in contract
                    and isinstance(plan_ref.get("files"), list)):
                legacy_targets.append(target_id)
                continue
            repository_keys = {
                "materialization_contract", "file_ledger_hash", "file_count",
                "total_bytes", "repository_snapshot_hash",
            }
            repository_hash = plan_ref.get("repository_snapshot_hash") if isinstance(
                plan_ref, dict) else None
            match = re.fullmatch(
                r"sha256:([0-9a-f]{64})", repository_hash
                if isinstance(repository_hash, str) else "")
            if (not isinstance(plan_ref, dict) or set(plan_ref) != repository_keys
                    or not isinstance(contract, dict) or match is None
                    or contract.get("repository_snapshot_hash")
                    != repository_hash
                    or not isinstance(plan_ref.get("file_ledger_hash"), str)
                    or _HASH_RE.fullmatch(plan_ref["file_ledger_hash"]) is None
                    or isinstance(plan_ref.get("file_count"), bool)
                    or not isinstance(plan_ref.get("file_count"), int)
                    or plan_ref["file_count"] < 1
                    or isinstance(plan_ref.get("total_bytes"), bool)
                    or not isinstance(plan_ref.get("total_bytes"), int)
                    or plan_ref["total_bytes"] < 0):
                raise EvidencePackError(
                    f"packed source import target {target_id} plan_ref shape 非法")
            digest = match.group(1)
            fact = {
                "contract": contract,
                "file_ledger_hash": plan_ref["file_ledger_hash"],
                "file_count": plan_ref["file_count"],
                "total_bytes": plan_ref["total_bytes"],
            }
            root = repository_roots.setdefault(
                digest, {**fact, "target_ids": []})
            if any(root[key] != fact[key] for key in fact):
                raise EvidencePackError(
                    f"packed source repository root {digest} contract 冲突")
            root["target_ids"].append(target_id)

        reported_targets = {}
        repositories = import_report.get("repository_objects")
        if not isinstance(repositories, list):
            raise EvidencePackError("packed import report repository_objects 非法")
        for value in repositories:
            digest = str(value.get("object_hash", "")).removeprefix(
                "sha256:") if isinstance(value, dict) else ""
            target_ids = value.get("target_ids") if isinstance(value, dict) else None
            if (not isinstance(target_ids, list)
                    or any(isinstance(item, bool) or not isinstance(item, int)
                           or item < 1 for item in target_ids)
                    or target_ids != sorted(set(target_ids))):
                raise EvidencePackError("packed import report target_ids 非法")
            reported_targets[digest] = target_ids
        expected_targets = {
            digest: value["target_ids"] for digest, value in repository_roots.items()}
        repository_target_count = sum(
            len(value) for value in expected_targets.values())
        dependency_capabilities: Dict[str, Dict[str, Any]] = {}
        for root in repository_roots.values():
            capability = root["contract"].get("execution_image")
            if capability is None:
                continue
            closure = capability.get("closure_hash") if isinstance(
                capability, dict) else None
            match = re.fullmatch(
                r"sha256:([0-9a-f]{64})", closure
                if isinstance(closure, str) else "")
            if match is None:
                raise EvidencePackError(
                    "packed source dependency capability closure 非法")
            digest = match.group(1)
            prior = dependency_capabilities.setdefault(digest, dict(capability))
            if prior != capability:
                raise EvidencePackError(
                    f"packed source dependency capability {digest} 冲突")
        expected_dependencies = [
            "sha256:" + digest for digest in sorted(dependency_capabilities)]
        if (reported_targets != expected_targets
                or import_report.get("import_targets") != import_target_count
                or import_report.get("repository_import_targets")
                != repository_target_count
                or import_report.get("legacy_import_targets") != legacy_targets
                or import_report.get("unbound_import_targets") != unbound_targets
                or import_report.get("dependency_objects")
                != expected_dependencies):
            raise EvidencePackError(
                "packed import report 与 source SQLite roots 漂移")
    except sqlite3.Error as error:
        raise EvidencePackError("packed source SQLite evidence 无法读取") from error
    finally:
        if connection is not None:
            connection.close()
        os.close(fd)

    indexes = [item for item in items.values() if item["kind"] == "log_mirror_index"]
    object_items = [item for item in items.values()
                    if item["kind"] == "log_mirror_object"]
    objects = {}
    for item in object_items:
        match = re.fullmatch(
            r"source/log-mirrors/objects/([0-9a-f]{64})\.gz",
            item["logical_id"])
        if match is None or match.group(1) in objects:
            raise EvidencePackError("packed log mirror object logical_id 非法/重复")
        objects[match.group(1)] = item
    referenced = set()
    seen_log_ids = set()
    for item in indexes:
        index_match = re.fullmatch(
            r"source/log-mirrors/indexes/execution-log-([1-9][0-9]*)\.json",
            item["logical_id"])
        if index_match is None:
            raise EvidencePackError("packed log mirror index logical_id 非法")
        log_id = int(index_match.group(1))
        if log_id in seen_log_ids or log_id not in registered:
            raise EvidencePackError("packed log mirror index id 重复/未登记")
        value = _strict_json(_object_bytes(pack, item), label="packed log mirror index")
        if (set(value) != {"schema", "execution_log", "mirror"}
                or value.get("schema") != LOG_MIRROR_SCHEMA
                or not isinstance(value.get("execution_log"), dict)
                or not isinstance(value.get("mirror"), dict)):
            raise EvidencePackError("packed log mirror index 字段非法")
        log = value["execution_log"]
        mirror = value["mirror"]
        digest = mirror.get("sha256")
        raw_digest = str(log.get("sha256", "")).removeprefix("sha256:")
        raw_bytes = log.get("bytes")
        if (set(mirror) != {"codec", "path", "sha256", "bytes"}
                or mirror.get("codec") != GZIP_PROFILE
                or not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None
                or not isinstance(raw_digest, str) or _HASH_RE.fullmatch(raw_digest) is None
                or log != registered[log_id]
                or isinstance(raw_bytes, bool) or not isinstance(raw_bytes, int)
                or raw_bytes < 0 or digest not in objects
                or mirror.get("path") != (
                    "state/storage/log-mirrors/objects/sha256/" + str(digest) + ".gz")
                or isinstance(mirror.get("bytes"), bool)
                or not isinstance(mirror.get("bytes"), int)
                or objects[digest]["sha256"] != digest
                or objects[digest]["bytes"] != mirror.get("bytes")):
            raise EvidencePackError("packed log mirror identity 非法")
        # Existing verifier's bounded gzip semantics remain the authority.  A
        # small local implementation would risk accepting multi-member/trailing
        # streams, so reconstruct its exact check against the packed object.
        import zlib
        fd = os.open(
            _object_path(pack, objects[digest]),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            raw_hash = hashlib.sha256()
            seen = 0
            ended = False
            header = bytearray()
            while True:
                block = os.read(fd, _COPY_BLOCK)
                if not block:
                    break
                if ended:
                    raise EvidencePackError("packed gzip 含尾随数据/多 member")
                if len(header) < 10:
                    header.extend(block[:10 - len(header)])
                pending = block
                while pending:
                    limit = min(_COPY_BLOCK, raw_bytes - seen + 1)
                    if limit <= 0:
                        raise EvidencePackError("packed gzip 解压越界")
                    before = len(pending)
                    output = decompressor.decompress(pending, limit)
                    raw_hash.update(output)
                    seen += len(output)
                    if seen > raw_bytes or decompressor.unused_data:
                        raise EvidencePackError("packed gzip 解压越界/多 member")
                    pending = decompressor.unconsumed_tail
                    if len(pending) == before and not output:
                        raise EvidencePackError("packed gzip 解压无进展")
                ended = decompressor.eof
            if (bytes(header) != bytes.fromhex("1f8b08000000000002ff")
                    or not decompressor.eof or decompressor.unconsumed_tail
                    or decompressor.unused_data or seen != raw_bytes
                    or raw_hash.hexdigest() != raw_digest):
                raise EvidencePackError("packed gzip raw identity 漂移")
        except zlib.error as error:
            raise EvidencePackError("packed gzip 损坏") from error
        finally:
            os.close(fd)
        referenced.add(digest)
        seen_log_ids.add(log_id)
    if seen_log_ids != set(registered):
        raise EvidencePackError("packed log mirror indexes 与 source DB 未 exact 闭合")
    if set(objects) != referenced:
        raise EvidencePackError("packed log mirror 含未登记 object")
    return len(indexes), repository_roots, dependency_capabilities


def _packed_json(
        pack: Path, items: Mapping[str, Mapping[str, Any]], logical_id: str,
        *, kind: str, label: str) -> Dict[str, Any]:
    item = items.get(logical_id)
    if item is None or item.get("kind") != kind:
        raise EvidencePackError(f"{label} item 缺失/类型非法")
    return _strict_json(_object_bytes(pack, item), label=label)


def _declared_file(
        item: Optional[Mapping[str, Any]], *, sha256: Any, size: Any,
        label: str) -> None:
    digest = sha256.removeprefix("sha256:") if isinstance(sha256, str) else ""
    if (item is None or _HASH_RE.fullmatch(digest) is None
            or isinstance(size, bool) or not isinstance(size, int) or size < 0
            or item.get("sha256") != digest or item.get("bytes") != size):
        raise EvidencePackError(f"{label} hash/bytes 闭包漂移")


def _validate_import_file_closure(
        pack: Path, items: Mapping[str, Mapping[str, Any]], *,
        repository_roots: Mapping[str, Mapping[str, Any]],
        dependency_capabilities: Mapping[str, Mapping[str, Any]]) -> None:
    """Bind every packed CAS file to DB plan facts or provider receipts."""
    for digest, root in repository_roots.items():
        base = f"source/import-materializations/objects/{digest}/"
        packed = {
            item["logical_id"][len(base):]: item for item in items.values()
            if item["kind"] == "import_repository_file"
            and item["logical_id"].startswith(base)}
        control_names = {
            "ledger.json", "spec.json", "transport.json", "receipt.json"}
        if not control_names.issubset(packed):
            raise EvidencePackError(
                f"packed repository {digest} control file 缺失")
        ledger = _packed_json_value(
            pack, packed["ledger.json"], label=f"packed repository {digest} ledger")
        spec = _packed_json_value(
            pack, packed["spec.json"], label=f"packed repository {digest} spec")
        transport = _packed_json_value(
            pack, packed["transport.json"],
            label=f"packed repository {digest} transport")
        receipt = _packed_json_value(
            pack, packed["receipt.json"],
            label=f"packed repository {digest} receipt")
        try:
            projected_ledger = spec_ledger({"file_ledger": ledger})
        except RuntimeError as error:
            raise EvidencePackError(
                f"packed repository {digest} ledger 无法投影") from error
        if (not isinstance(spec, dict) or not isinstance(transport, list)
                or not isinstance(receipt, dict)
                or ledger != sorted(
                    ledger, key=lambda value: value.get("path", "")
                    if isinstance(value, dict) else "")
                or canonical_hash(projected_ledger) != root.get("file_ledger_hash")
                or len(projected_ledger) != root.get("file_count")
                or sum(value["bytes"] for value in projected_ledger)
                != root.get("total_bytes")):
            raise EvidencePackError(
                f"packed repository {digest} ledger/DB contract 漂移")
        expected_paths = set(control_names)
        seen_paths = set()
        for value in ledger:
            path = value.get("path") if isinstance(value, dict) else None
            sha256 = value.get("sha256") if isinstance(value, dict) else None
            size = value.get("bytes") if isinstance(value, dict) else None
            if (not isinstance(path, str) or not path or path in seen_paths
                    or path.startswith("/") or "\\" in path
                    or any(part in {"", ".", ".."} for part in path.split("/"))):
                raise EvidencePackError(
                    f"packed repository {digest} ledger path 非法")
            seen_paths.add(path)
            relative = "tree/" + path
            _declared_file(
                packed.get(relative), sha256=sha256, size=size,
                label=f"packed repository {digest}/{relative}")
            expected_paths.add(relative)
        if set(packed) != expected_paths:
            raise EvidencePackError(
                f"packed repository {digest} 文件闭包缺失/多余")
        try:
            projected = execution_contract(spec)
            projected["repository_snapshot_hash"] = "sha256:" + digest
        except (KeyError, TypeError, ValueError) as error:
            raise EvidencePackError(
                f"packed repository {digest} spec 无法投影") from error
        ledger_value_hash = "sha256:" + _hash(_canonical(ledger))
        if (projected != root.get("contract")
                or receipt.get("object_hash") != "sha256:" + digest
                or receipt.get("file_ledger_hash") != ledger_value_hash
                or receipt.get("file_count") != len(ledger)
                or receipt.get("total_bytes") != root.get("total_bytes")
                or receipt.get("spec_hash") != "sha256:" + _hash(_canonical(spec))
                or receipt.get("transport_evidence_hash")
                != "sha256:" + _hash(_canonical(transport))):
            raise EvidencePackError(
                f"packed repository {digest} receipt/spec 绑定漂移")

    for digest, capability in dependency_capabilities.items():
        base = (
            "source/import-materializations/dependency-images/objects/"
            f"{digest}/")
        packed = {
            item["logical_id"][len(base):]: item for item in items.values()
            if item["kind"] == "dependency_image_file"
            and item["logical_id"].startswith(base)}
        receipt_item = packed.get("receipt.json")
        if receipt_item is None:
            raise EvidencePackError(
                f"packed dependency {digest} receipt 缺失")
        receipt = _packed_json_value(
            pack, receipt_item, label=f"packed dependency {digest} receipt")
        if not isinstance(receipt, dict):
            raise EvidencePackError(f"packed dependency {digest} receipt 非 object")
        expected_capability = {
            "version": 1, "provider": receipt.get("provider"),
            "closure_hash": receipt.get("closure_hash"),
            "receipt_hash": "sha256:" + _hash(_canonical(receipt)),
            "environment_hash": receipt.get("environment_hash"),
            "image": receipt.get("result_image_id"),
            "image_id": receipt.get("result_image_id"),
        }
        lock = receipt.get("lock")
        wheels = receipt.get("wheels")
        runtime = receipt.get("runtime")
        archive = receipt.get("image_archive")
        if (dict(capability) != expected_capability
                or receipt.get("closure_hash") != "sha256:" + digest
                or not isinstance(lock, dict) or not isinstance(wheels, list)
                or not wheels or not isinstance(runtime, dict)
                or not isinstance(archive, dict)):
            raise EvidencePackError(
                f"packed dependency {digest} capability/receipt 漂移")
        expected_paths = {"receipt.json"}
        lock_path = lock.get("path")
        lock_path = _dependency_relative_path(
            lock_path, label=f"packed dependency {digest} lock source path")
        lock_name = PurePosixPath(lock_path).name
        if lock_name != "python-wheel-lock.json":
            raise EvidencePackError(f"packed dependency {digest} lock basename 非法")
        _declared_file(
            packed.get(lock_name), sha256=lock.get("sha256"),
            size=lock.get("bytes"), label=f"packed dependency {digest} lock")
        expected_paths.add(lock_name)
        for wheel in wheels:
            if not isinstance(wheel, dict) or not isinstance(wheel.get("filename"), str):
                raise EvidencePackError(f"packed dependency {digest} wheel receipt 非法")
            relative = "wheelhouse/" + wheel["filename"]
            _declared_file(
                packed.get(relative), sha256=wheel.get("sha256"),
                size=wheel.get("bytes"), label=f"packed dependency {digest} wheel")
            expected_paths.add(relative)

        manifest_item = packed.get("installed-manifest.json")
        if manifest_item is None:
            raise EvidencePackError(
                f"packed dependency {digest} installed manifest 缺失")
        installed = _packed_json_value(
            pack, manifest_item,
            label=f"packed dependency {digest} installed manifest")
        files = installed.get("files") if isinstance(installed, dict) else None
        if (not isinstance(files, list) or not files
                or installed.get("manifest_hash")
                != "sha256:" + _hash(_canonical(files))
                or receipt.get("install_manifest_hash")
                != "sha256:" + _hash(_canonical(files))):
            raise EvidencePackError(
                f"packed dependency {digest} installed manifest 漂移")
        expected_paths.add("installed-manifest.json")
        for value in files:
            path = value.get("path") if isinstance(value, dict) else None
            if (not isinstance(path, str) or not path or path.startswith("/")
                    or "\\" in path
                    or any(part in {"", ".", ".."} for part in path.split("/"))):
                raise EvidencePackError(
                    f"packed dependency {digest} installed path 非法")
            for prefix in ("install/site-packages/", "context/site-packages/"):
                relative = prefix + path
                _declared_file(
                    packed.get(relative), sha256=value.get("sha256"),
                    size=value.get("bytes"),
                    label=f"packed dependency {digest}/{relative}")
                expected_paths.add(relative)

        fixed_hashes = {
            "runtime/runtime.json": runtime.get("runtime_output_sha256"),
            "runtime/runtime.log": runtime.get("runtime_log_sha256"),
            "check/pip-check.log": runtime.get("pip_check_log_sha256"),
            "context/Dockerfile": receipt.get("dockerfile_sha256"),
            "image.tar": archive.get("sha256"),
        }
        for relative, sha256 in fixed_hashes.items():
            item = packed.get(relative)
            size = archive.get("bytes") if relative == "image.tar" else (
                item.get("bytes") if item is not None else None)
            _declared_file(
                item, sha256=sha256, size=size,
                label=f"packed dependency {digest}/{relative}")
            expected_paths.add(relative)
        for relative in ("runtime/runtime.log.exit", "check/pip-check.log.exit"):
            item = packed.get(relative)
            if item is None or item.get("sha256") != _hash(b"0") or item.get("bytes") != 1:
                raise EvidencePackError(
                    f"packed dependency {digest}/{relative} 漂移")
            expected_paths.add(relative)
        if expected_paths != _dependency_semantic_paths(receipt, installed):
            raise EvidencePackError(
                f"packed dependency {digest} semantic path 投影漂移")
        if set(packed) != expected_paths:
            raise EvidencePackError(
                f"packed dependency {digest} 文件闭包缺失/多余")


def _validate_domain_reports(
        pack: Path, items: Mapping[str, Mapping[str, Any]], *,
        source_cycle: int, source_storage: Mapping[str, Any],
        storage_report: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate each fixed evidence domain without manifest-side labels."""
    source_cycle_name = f"c{source_cycle}"
    expected_storage_ids = {
        "source/storage/verify.json", "source/storage/genesis.json",
        f"source/storage/c{source_cycle}.pointer.json",
        f"source/storage/c{source_cycle}.manifest.json",
        f"source/storage/c{source_cycle}.sqlite",
    }
    if ({logical_id for logical_id in items
         if logical_id.startswith("source/storage/")} != expected_storage_ids):
        raise EvidencePackError("source storage evidence 逻辑闭包漂移")
    genesis = _packed_json(
        pack, items, "source/storage/genesis.json", kind="storage_genesis",
        label="packed source storage genesis")
    coverage = _cycle(
        storage_report.get("coverage_start_cycle"), label="source coverage")
    if (storage_report.get("schema") != "meta-research-storage-verify/v1"
            or storage_report.get("scope")
            != "snapshot_chain_and_retained_sqlite"
            or storage_report.get("high_water_cycle") != source_cycle_name
            or storage_report.get("high_water_manifest_sha256")
            != source_storage["pointer"]["manifest_sha256"]
            or storage_report.get("views_commit")
            != source_storage["manifest"].get("views", {}).get("commit")
            or not isinstance(storage_report.get("deep_verified_cycles"), list)
            or source_cycle_name not in storage_report["deep_verified_cycles"]
            or coverage > source_cycle
            or set(genesis) != {
                "schema", "coverage_start_cycle", "adoption_baseline",
                "bootstrap_before_cycle"}
            or genesis.get("schema") != sg.GENESIS_SCHEMA
            or genesis.get("coverage_start_cycle") != coverage
            or not isinstance(genesis.get("adoption_baseline"), bool)
            or (source_cycle == coverage and (
                genesis.get("adoption_baseline")
                is not source_storage["manifest"].get("adoption_baseline")
                or genesis.get("bootstrap_before_cycle")
                != source_storage["manifest"].get("bootstrap_before_cycle")))):
        raise EvidencePackError("packed source storage report/genesis 漂移")

    log_report = _packed_json(
        pack, items, "source/log-mirrors/verify.json", kind="report",
        label="packed log mirror report")
    import_report = _packed_json(
        pack, items, "source/import-materializations/verify.json", kind="report",
        label="packed import materialization report")
    for report, schema, label in (
            (log_report, "meta-research-log-mirror-report/v1", "log mirror"),
            (import_report, "meta-research-import-materialization-verify/v1",
             "import materialization")):
        if (report.get("schema") != schema
                or report.get("high_water_cycle") != source_cycle_name
                or report.get("high_water_manifest_sha256")
                != source_storage["pointer"]["manifest_sha256"]):
            raise EvidencePackError(f"packed {label} report high-water 漂移")

    log_domain = [item for item in items.values()
                  if item["logical_id"].startswith("source/log-mirrors/")]
    index_items = [item for item in log_domain
                   if item["kind"] == "log_mirror_index"]
    report_ids = {item["logical_id"] for item in log_domain
                  if item["kind"] == "report"}
    if (report_ids != {"source/log-mirrors/verify.json"}
            or log_report.get("scope") != "db_registered_execution_logs_only"
            or log_report.get("registered_logs") != len(index_items)
            or log_report.get("originals_verified") != len(index_items)
            or log_report.get("mirrors_verified") != len(index_items)):
        raise EvidencePackError("packed log mirror report/count 漂移")

    repositories = import_report.get("repository_objects")
    dependencies = import_report.get("dependency_objects")
    if not isinstance(repositories, list) or not isinstance(dependencies, list):
        raise EvidencePackError("packed import report 集合非法")
    repository_digests = set()
    dependency_digests = set()
    expected_index_roots: Dict[str, str] = {}
    for value in repositories:
        if (not isinstance(value, dict)
                or set(value) != {"object_hash", "target_ids", "indexes"}
                or not isinstance(value.get("indexes"), list)):
            raise EvidencePackError("packed import repository report 非法")
        digest = str(value.get("object_hash", "")).removeprefix("sha256:")
        if (_HASH_RE.fullmatch(digest) is None
                or digest in repository_digests):
            raise EvidencePackError("packed import repository digest 非法/重复")
        repository_digests.add(digest)
        for name in value["indexes"]:
            if (not isinstance(name, str)
                    or re.fullmatch(r"[0-9a-f]{64}\.json", name) is None):
                raise EvidencePackError("packed import index name 非法")
            if name in expected_index_roots:
                raise EvidencePackError("packed import index 被多个 object 引用")
            expected_index_roots[name] = digest
    for value in dependencies:
        digest = value.removeprefix("sha256:") if isinstance(value, str) else ""
        if (_HASH_RE.fullmatch(digest) is None
                or digest in dependency_digests):
            raise EvidencePackError("packed dependency digest 非法/重复")
        dependency_digests.add(digest)
    import_domain = [item for item in items.values()
                     if item["logical_id"].startswith(
                         "source/import-materializations/")]
    if ({item["logical_id"] for item in import_domain
         if item["kind"] == "report"}
            != {"source/import-materializations/verify.json"}
            or import_report.get("scope")
            != "sqlite_registered_repository_and_dependency_cas"):
        raise EvidencePackError("packed import report shape 非法")
    actual_indexes = {
        item["logical_id"].removeprefix(
            "source/import-materializations/indexes/")
        for item in import_domain if item["kind"] == "import_index"}
    if actual_indexes != set(expected_index_roots):
        raise EvidencePackError("packed import indexes 与 report 漂移")
    for item in import_domain:
        if item["kind"] != "import_index":
            continue
        name = item["logical_id"].removeprefix(
            "source/import-materializations/indexes/")
        value = _strict_json(_object_bytes(pack, item), label="packed import index")
        if (value.get("version") != 1
                or value.get("object_hash")
                != "sha256:" + expected_index_roots[name]):
            raise EvidencePackError("packed import index/object binding 漂移")
    for kind, base, digests in (
            ("import_repository_file", "source/import-materializations/objects/",
             repository_digests),
            ("dependency_image_file",
             "source/import-materializations/dependency-images/objects/",
             dependency_digests)):
        seen = set()
        for item in import_domain:
            if item["kind"] != kind:
                continue
            if not item["logical_id"].startswith(base):
                raise EvidencePackError("packed import object path 与 report 漂移")
            digest, separator, _relative = item["logical_id"][len(base):].partition("/")
            if not separator or digest not in digests:
                raise EvidencePackError("packed import object path 与 report 漂移")
            seen.add(digest)
        if seen != digests:
            raise EvidencePackError("packed import object closure 缺失")

    qualification_ids = {
        logical_id for logical_id in items
        if logical_id.startswith("source/qualification/")}
    if qualification_ids:
        qualification_reports = {
            logical_id for logical_id in qualification_ids
            if items[logical_id]["kind"] == "report"}
        report = _packed_json(
            pack, items, "source/qualification/verify.json", kind="report",
            label="packed qualification report")
        contract = items.get("source/qualification/state/contract.json")
        if (qualification_reports != {"source/qualification/verify.json"}
                or report.get("status") != "receipt_only"
                or report.get("qualification_complete_claimed") is not False
                or contract is None
                or contract.get("kind") != "qualification_receipt"
                or contract.get("sha256") != report.get("contract_sha256")):
            raise EvidencePackError("qualification receipt-only 边界漂移")

    canary_ids = {
        logical_id for logical_id in items
        if logical_id.startswith("shared-fs-canary/")}
    if canary_ids:
        canary_reports = {
            logical_id for logical_id in canary_ids
            if items[logical_id]["kind"] == "report"}
        report = _packed_json(
            pack, items, "shared-fs-canary/verify.json", kind="report",
            label="packed shared-fs canary report")
        finals = [item for item in items.values()
                  if item["kind"] == "canary_receipt"
                  and item["logical_id"].startswith("shared-fs-canary/")
                  and item["logical_id"].rsplit("/", 1)[-1]
                  in {"final.json", "final-local.json"}]
        sqlite_items = [item["logical_id"] for item in items.values()
                        if item["kind"] == "sqlite_snapshot"
                        and item["logical_id"].startswith("shared-fs-canary/")]
        if (canary_reports != {"shared-fs-canary/verify.json"}
                or report.get("status") != "passed"
                or report.get("infrastructure_fence_verified") is not False
                or len(finals) != 1
                or _strict_json(_object_bytes(pack, finals[0]),
                                label="packed canary final") != report
                or sqlite_items not in ([], ["shared-fs-canary/research.sqlite"])):
            raise EvidencePackError("shared-fs canary final/report 漂移")

    fault_ids = {
        logical_id for logical_id in items
        if logical_id.startswith("source/fault-schedules/")}
    if fault_ids:
        schedules = set()
        report_schedules = set()
        for logical_id in fault_ids:
            parts = logical_id.split("/")
            if len(parts) < 4 or _SCHEDULE_RE.fullmatch(parts[2]) is None:
                raise EvidencePackError("fault schedule logical_id 非法")
            schedule_id = parts[2]
            schedules.add(schedule_id)
            if items[logical_id]["kind"] == "report":
                if parts[3:] != ["verify.json"]:
                    raise EvidencePackError("fault schedule report logical_id 非法")
                report_schedules.add(schedule_id)
            elif parts[3] != "state" or len(parts) < 5:
                raise EvidencePackError("fault schedule receipt logical_id 非法")
        if report_schedules != schedules:
            raise EvidencePackError("fault schedules 缺 verify report")
        for schedule_id in sorted(schedules):
            report = _packed_json(
                pack, items,
                f"source/fault-schedules/{schedule_id}/verify.json",
                kind="report", label="packed fault report")
            final = items.get(
                f"source/fault-schedules/{schedule_id}/state/final.json")
            if (final is None or final.get("kind") != "fault_receipt"
                    or _strict_json(_object_bytes(pack, final),
                                    label="packed fault final") != report
                    or report.get("status") != "complete"
                    or report.get("signal_exactly_once") is not False
                    or report.get("recovery_verified") is not False):
                raise EvidencePackError("fault final/report honesty binding 漂移")
    return import_report


def _validate_item_domains(items: Mapping[str, Mapping[str, Any]]) -> None:
    rules = (
        ("source/storage/", {
            "report", "storage_genesis", "storage_pointer",
            "storage_manifest", "sqlite_snapshot"}),
        ("source/log-mirrors/", {
            "report", "log_mirror_index", "log_mirror_object"}),
        ("source/import-materializations/", {
            "report", "import_index", "import_repository_file",
            "dependency_image_file"}),
        ("source/fault-schedules/", {"report", "fault_receipt"}),
        ("source/qualification/", {"report", "qualification_receipt"}),
        ("shared-fs-canary/", {
            "report", "canary_receipt", "sqlite_snapshot"}),
        ("resume/", {
            "report", "storage_genesis", "storage_pointer",
            "storage_manifest", "sqlite_snapshot", "restore_receipt"}),
    )
    for logical_id, item in items.items():
        matches = [allowed for prefix, allowed in rules
                   if logical_id.startswith(prefix)]
        if len(matches) != 1 or item["kind"] not in matches[0]:
            raise EvidencePackError(
                f"evidence item 逻辑域/类型非法: {logical_id}")


def _verify_evidence_pack_anchored(
        pack: Path, *, root_fd: int, expected_manifest_hash: str,
        return_identities: bool) -> Dict[str, Any]:
    """Verify descendants relative to one already-pinned pack root fd."""
    expected_root = {"manifest.json", "READY.json", "objects"}
    if {path.name for path in pack.iterdir()} != expected_root:
        raise EvidencePackError("evidence pack 根目录闭包漂移")
    objects_parent = pack / "objects"
    _safe_directory(objects_parent, label="evidence objects", private=True)
    if {path.name for path in objects_parent.iterdir()} != {"sha256"}:
        raise EvidencePackError("evidence objects 布局漂移")
    objects = objects_parent / "sha256"
    _safe_directory(objects, label="evidence sha256 objects", private=True)
    for control_name in ("manifest.json", "READY.json"):
        control = pack / control_name
        info = control.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o400):
            raise EvidencePackError(f"evidence {control_name} authority 非法")

    manifest_raw = _read_regular(
        pack / "manifest.json", label="evidence manifest", maximum=_MAX_JSON_BYTES)
    manifest = _strict_json(manifest_raw, label="evidence manifest")
    manifest_hash = _hash(manifest_raw)
    ready_raw = _read_regular(pack / "READY.json", label="evidence READY")
    ready = _strict_json(ready_raw, label="evidence READY")
    if (manifest_hash != expected_manifest_hash
            or set(ready) != {"version", "protocol", "manifest_sha256"}
            or ready.get("version") != 1
            or ready.get("protocol") != READY_PROTOCOL
            or ready.get("manifest_sha256") != manifest_hash):
        raise EvidencePackError("evidence manifest/READY/目录名绑定漂移")
    file_identities = {
        ".": _file_identity(os.fstat(root_fd)),
        "objects": _file_identity(objects_parent.lstat()),
        "objects/sha256": _file_identity(objects.lstat()),
        "manifest.json": _verify_pack_file(
            pack / "manifest.json", label="evidence manifest",
            expected_hash=manifest_hash, expected_bytes=len(manifest_raw)),
        "READY.json": _verify_pack_file(
            pack / "READY.json", label="evidence READY",
            expected_hash=_hash(ready_raw), expected_bytes=len(ready_raw)),
    }
    if set(manifest) != {
            "version", "protocol", "source_work_root", "resume_probe", "items"}:
        raise EvidencePackError("evidence manifest 顶层字段闭包漂移")
    source_work_root = manifest.get("source_work_root")
    if (manifest.get("version") != 1
            or manifest.get("protocol") != PACK_PROTOCOL
            or not isinstance(source_work_root, str)
            or not source_work_root
            or len(source_work_root.encode("utf-8", errors="ignore")) > 8192
            or any(ord(char) < 0x20 or ord(char) == 0x7f
                   for char in source_work_root)
            or not os.path.isabs(source_work_root)
            or str(Path(os.path.abspath(source_work_root))) != source_work_root):
        raise EvidencePackError("evidence manifest 协议/source_work_root 非法")

    items = _item_map(manifest)
    _validate_item_domains(items)
    expected_digests = {item["sha256"] for item in items.values()}
    digest_sizes: Dict[str, int] = {}
    for item in items.values():
        prior_size = digest_sizes.setdefault(item["sha256"], item["bytes"])
        if prior_size != item["bytes"]:
            raise EvidencePackError("evidence duplicate object bytes 声明冲突")
    if len(items) > 1_000_000:
        raise EvidencePackError("evidence object/item 数超过 verifier 硬上限")
    actual_names = set()
    for path in objects.iterdir():
        if len(actual_names) >= 1_000_000:
            raise EvidencePackError("evidence object 数超过 verifier 硬上限")
        actual_names.add(path.name)
    if actual_names != expected_digests:
        raise EvidencePackError("evidence object 集缺失/多余")
    total = 0
    for digest in sorted(actual_names):
        if _HASH_RE.fullmatch(digest) is None:
            raise EvidencePackError("evidence object 文件名非法")
        size = digest_sizes[digest]
        file_identities[f"objects/sha256/{digest}"] = _verify_pack_file(
            objects / digest, label=f"evidence object {digest}",
            expected_hash=digest, expected_bytes=size)
        total += size
        if total + len(manifest_raw) + len(ready_raw) > 1024 ** 4:
            raise EvidencePackError("evidence pack 总字节超过 verifier 硬上限")

    storage_report = _packed_json(
        pack, items, "source/storage/verify.json", kind="report",
        label="packed source storage report")
    source_cycle = _cycle(
        storage_report.get("high_water_cycle"), label="source high-water")
    source_storage = _validate_storage_object(
        pack, items, prefix="source", cycle_id=source_cycle,
        allow_later_cycles=False)
    if (storage_report.get("high_water_manifest_sha256")
            != source_storage["pointer"]["manifest_sha256"]):
        raise EvidencePackError("source high-water 与 storage item 漂移")
    import_report = _validate_domain_reports(
        pack, items, source_cycle=source_cycle,
        source_storage=source_storage, storage_report=storage_report)
    resume_verified = _validate_resume_offline(
        pack, manifest.get("resume_probe"), items,
        source_cycle=source_cycle, source_work_root=source_work_root,
        source_storage=source_storage)
    log_count, repository_roots, dependency_capabilities = (
        _validate_log_mirrors_offline(
        pack, items, source_cycle=source_cycle,
        source_work_root=source_work_root,
        source_assets=source_storage["manifest"]["assets"],
        import_report=import_report))
    _validate_import_file_closure(
        pack, items, repository_roots=repository_roots,
        dependency_capabilities=dependency_capabilities)
    unresolved = [asset for asset in source_storage["manifest"].get("assets", [])
                  if isinstance(asset, dict)
                  and asset.get("owner") in {"checkpoint", "execution_log"}]

    _assert_pack_unchanged(pack, file_identities, root_fd=root_fd)
    result = {
        "version": 1, "protocol": PACK_PROTOCOL,
        "status": "verified", "manifest_sha256": manifest_hash,
        "source_cycle": f"c{source_cycle}",
        "pack_integrity_verified": True,
        "one_cycle_resume_probe_verified": resume_verified,
        "real_codex_resume_verified": False,
        "qualification_receipts_verified": False,
        "full_restore_verified": False,
        "objects": len(actual_names), "items": len(items),
        "bytes": total + len(manifest_raw) + len(ready_raw),
        "log_mirrors_verified": log_count,
        "unresolved_registered_assets": len(unresolved),
    }
    if return_identities:
        result["_verified_file_identities"] = file_identities
    return result


def verify_evidence_pack(
        pack_path: Path | str, *,
        _expected_manifest_sha256: Optional[str] = None) -> Dict[str, Any]:
    """Pure read-only verification pinned to the directory opened at entry."""
    display_path = Path(os.path.abspath(os.fspath(pack_path)))
    _canonical_directory(display_path, label="evidence pack", private=True)
    if _expected_manifest_sha256 is None:
        match = re.fullmatch(r"([0-9a-f]{64})\.evidence", display_path.name)
        if match is None:
            raise EvidencePackError("evidence pack 目录名须为 manifest hash")
        expected_manifest_hash = match.group(1)
        return_identities = False
    else:
        if (not isinstance(_expected_manifest_sha256, str)
                or _HASH_RE.fullmatch(_expected_manifest_sha256) is None
                or not display_path.name.startswith(".evidence-pack-")):
            raise EvidencePackError("private evidence staging identity 非法")
        expected_manifest_hash = _expected_manifest_sha256
        return_identities = True

    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        root_fd = os.open(display_path, flags)
    except OSError as error:
        raise EvidencePackError("evidence pack root 无法固定") from error
    try:
        root_info = os.fstat(root_fd)
        path_info = display_path.lstat()
        if (not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != os.geteuid()
                or stat.S_IMODE(root_info.st_mode) != 0o700
                or (root_info.st_dev, root_info.st_ino)
                != (path_info.st_dev, path_info.st_ino)):
            raise EvidencePackError("evidence pack root 打开期间漂移")
        anchored = Path(f"/proc/self/fd/{root_fd}")
        return _verify_evidence_pack_anchored(
            anchored, root_fd=root_fd,
            expected_manifest_hash=expected_manifest_hash,
            return_identities=return_identities)
    finally:
        os.close(root_fd)


def _print(value: Mapping[str, Any], *, stream=None) -> None:
    print(json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False), file=stream)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="meta-research canonical evidence pack / offline verifier")
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("pack")
    pack.add_argument("--source-work-root", required=True)
    pack.add_argument("--resume-work-root")
    pack.add_argument("--output-parent", required=True)
    pack.add_argument("--canary-root")
    pack.add_argument("--canary-run-id")
    pack.add_argument(
        "--canary-scope", choices=(LOCAL_SCOPE, TWO_NODE_SCOPE), default=LOCAL_SCOPE)
    pack.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    pack.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    verify = commands.add_parser("verify")
    verify.add_argument("--pack", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            result = create_evidence_pack(
                source_work_root=args.source_work_root,
                resume_work_root=args.resume_work_root,
                output_parent=args.output_parent,
                canary_root=args.canary_root,
                canary_run_id=args.canary_run_id,
                canary_scope=args.canary_scope,
                max_files=args.max_files, max_bytes=args.max_bytes)
        else:
            result = verify_evidence_pack(args.pack)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        _print({
            "status": "unsafe",
            "error": f"{type(error).__name__}: {error}"[:500],
        }, stream=sys.stderr)
        return 3
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EvidencePackError", "create_evidence_pack", "verify_evidence_pack", "main",
]
