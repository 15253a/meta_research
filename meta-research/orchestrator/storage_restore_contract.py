"""Shared fail-closed contract for multi-slice storage restore continuations."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple


IMPORT_RESTORE_MARKER = b"meta-research-import-materialization-restore/v1\n"
REGISTERED_RESTORE_MARKER = b"meta-research-registered-asset-restore/v1\n"
CONTINUATION_MODES = {
    IMPORT_RESTORE_MARKER: "import_materialization_restore_required",
    REGISTERED_RESTORE_MARKER: "registered_asset_restore_required",
}
REGISTERED_RESTORE_SCHEMA = "meta-research-registered-asset-restore/v1"
REGISTERED_COMPLETION_RELATIVE = Path(
    "state/storage/registered-assets/restore.json")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CYCLE_RE = re.compile(r"^c[1-9][0-9]*$")
_MAX_RECEIPT_BYTES = 64 * 1024 * 1024


class StorageRestoreContractError(RuntimeError):
    """A continuation marker or registered completion authority is invalid."""


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def continuation_mode(marker: bytes | None) -> str:
    if marker is None:
        return "legacy_adoption_on_first_start"
    try:
        return CONTINUATION_MODES[marker]
    except (KeyError, TypeError) as error:
        raise StorageRestoreContractError("未知 storage restore continuation marker") from error


def read_marker(target: Path) -> bytes | None:
    marker_path = target / ".restore-in-progress"
    if not os.path.lexists(marker_path):
        return None
    try:
        fd = os.open(
            marker_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise StorageRestoreContractError("storage restore marker 不可打开") from error
    try:
        info = os.fstat(fd)
        path_info = marker_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o400
                or (info.st_dev, info.st_ino)
                != (path_info.st_dev, path_info.st_ino)
                or info.st_size < 1 or info.st_size > 1024):
            raise StorageRestoreContractError("storage restore marker authority 非法")
        raw = bytearray()
        while len(raw) < info.st_size:
            block = os.read(fd, info.st_size - len(raw))
            if not block:
                raise StorageRestoreContractError("storage restore marker 提前 EOF")
            raw.extend(block)
    finally:
        os.close(fd)
    value = bytes(raw)
    if value not in CONTINUATION_MODES:
        raise StorageRestoreContractError("storage restore marker protocol 非法")
    return value


def _read_canonical_object(path: Path) -> Dict[str, Any]:
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise StorageRestoreContractError(
            "registered restore completion receipt 缺失") from error
    try:
        info = os.fstat(fd)
        path_info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o400
                or (info.st_dev, info.st_ino)
                != (path_info.st_dev, path_info.st_ino)
                or info.st_size < 2 or info.st_size > _MAX_RECEIPT_BYTES):
            raise StorageRestoreContractError(
                "registered restore completion receipt authority 非法")
        raw = bytearray()
        while len(raw) < info.st_size:
            block = os.read(fd, min(1024 * 1024, info.st_size - len(raw)))
            if not block:
                raise StorageRestoreContractError(
                    "registered restore completion receipt 提前 EOF")
            raw.extend(block)
    finally:
        os.close(fd)
    try:
        value = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StorageRestoreContractError(
            "registered restore completion receipt JSON 损坏") from error
    if not isinstance(value, dict) or canonical(value) != bytes(raw):
        raise StorageRestoreContractError(
            "registered restore completion receipt 非 canonical object")
    return value


def _safe_relative(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise StorageRestoreContractError("registered completion relative_path 非法")
    path = Path(value)
    if (path.is_absolute() or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.parts[:3] == ("state", "storage", "registered-assets")):
        raise StorageRestoreContractError("registered completion relative_path 越界")
    return path


def _verify_file(path: Path, *, digest: str, n_bytes: int) -> None:
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise StorageRestoreContractError(
            f"registered hydrated file 缺失: {path}") from error
    try:
        info = os.fstat(fd)
        path_info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o400
                or info.st_size != n_bytes
                or (info.st_dev, info.st_ino)
                != (path_info.st_dev, path_info.st_ino)):
            raise StorageRestoreContractError(
                f"registered hydrated file authority 漂移: {path}")
        computed = hashlib.sha256()
        total = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            computed.update(block)
            total += len(block)
        if (computed.hexdigest(), total) != (digest, n_bytes):
            raise StorageRestoreContractError(
                f"registered hydrated file hash/bytes 漂移: {path}")
    finally:
        os.close(fd)


def validate_registered_completion(
        target: Path | str, *, source_work_root: str,
        source_cycle: str, source_manifest_sha256: str) -> Tuple[
            Dict[str, Any], Dict[str, Tuple[str, int]]]:
    """Validate the immutable receipt and every hydrated target file."""
    root = Path(os.path.abspath(os.fspath(target)))
    value = _read_canonical_object(root / REGISTERED_COMPLETION_RELATIVE)
    if (set(value) != {
            "schema", "scope", "source_work_root", "source_cycle",
            "source_manifest_sha256", "files"}
            or value.get("schema") != REGISTERED_RESTORE_SCHEMA
            or value.get("scope")
            != "db_registered_checkpoints_and_execution_logs"
            or value.get("source_work_root") != source_work_root
            or value.get("source_cycle") != source_cycle
            or not _CYCLE_RE.fullmatch(str(value.get("source_cycle", "")))
            or value.get("source_manifest_sha256") != source_manifest_sha256
            or not _HASH_RE.fullmatch(
                str(value.get("source_manifest_sha256", "")))
            or not isinstance(value.get("files"), list)):
        raise StorageRestoreContractError(
            "registered restore completion receipt source identity 漂移")
    identities: Dict[str, Tuple[str, int]] = {}
    previous = None
    for item in value["files"]:
        if (not isinstance(item, dict)
                or set(item) != {
                    "kind", "owner_id", "relative_path", "sha256", "bytes",
                    "mirror_path", "mirror_sha256", "mirror_bytes"}
                or item.get("kind") not in {"checkpoint", "execution_log"}
                or isinstance(item.get("owner_id"), bool)
                or not isinstance(item.get("owner_id"), int)
                or item["owner_id"] < 1
                or not _HASH_RE.fullmatch(str(item.get("sha256", "")))
                or isinstance(item.get("bytes"), bool)
                or not isinstance(item.get("bytes"), int) or item["bytes"] < 0
                or not isinstance(item.get("mirror_path"), str)
                or not _HASH_RE.fullmatch(str(item.get("mirror_sha256", "")))
                or isinstance(item.get("mirror_bytes"), bool)
                or not isinstance(item.get("mirror_bytes"), int)
                or item["mirror_bytes"] < 0):
            raise StorageRestoreContractError(
                "registered restore completion file identity 非法")
        relative = _safe_relative(item["relative_path"])
        order = (item["relative_path"], item["kind"], item["owner_id"])
        if previous is not None and order <= previous:
            raise StorageRestoreContractError(
                "registered restore completion files 未严格 canonical 排序")
        previous = order
        identity = (item["sha256"], item["bytes"])
        prior = identities.setdefault(item["relative_path"], identity)
        if prior != identity:
            raise StorageRestoreContractError(
                "registered restore completion 同路径身份冲突")
        _verify_file(root / relative, digest=item["sha256"], n_bytes=item["bytes"])
    return value, identities
