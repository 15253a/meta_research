"""Shared identities and bounded primitives for repository materialization."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional, Sequence

_PROTOCOL = "github-repository-snapshot-v1"
_ADAPTER_VERSION = 2
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FULL_NAME_RE = re.compile(
    r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_GITHUB_URI_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})$")
_LOG_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_ARTIFACT_TYPES = frozenset({
    "checkpoint", "external_model", "prompt_only", "algorithm",
    "retrieval_index",
})
_LFS_VERSION = "https://git-lfs.github.com/spec/v1"
_MAX_ADAPTER_BYTES = 1024 * 1024
_MAX_GITMODULES_BYTES = 1024 * 1024
_MAX_SEARCH_SNAPSHOT_BYTES = 2 * 1024 * 1024
_MAX_RECEIPT_BYTES = 128 * 1024 * 1024
_CONTROL_ENV_KEYS = (
    "HOME", "LANG", "LC_ALL", "PATH", "PYTHONDONTWRITEBYTECODE",
)


class RepositoryMaterializationError(ValueError):
    """The frozen repository cannot be materialized without guessing."""


class RepositoryTransportError(RuntimeError):
    """Retryable provider/runtime transport failure; never settle a candidate."""


class RepositoryCacheError(RuntimeError):
    """Local authority/cache corruption; never blame or settle the candidate."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False) + "\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _value_hash(value: Any) -> str:
    return _sha256(_canonical(value))


def _atomic_write_json(path: Path, value: Any, *, maximum: int) -> None:
    """Publish a bounded canonical JSON object under an already trusted parent."""
    try:
        payload = _canonical(value)
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise RepositoryMaterializationError(
            f"{path.name} 不是有限 JSON") from error
    if len(payload) > maximum:
        raise RepositoryMaterializationError(
            f"{path.name} 超过 {maximum} bytes")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    directory_fd = os.open(
        path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    temporary = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = -1
    try:
        info = os.fstat(directory_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise RepositoryCacheError(
                f"{path.name} parent authority 非法")
        fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("canonical JSON short write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(
            temporary, path.name, src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        os.close(directory_fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise RepositoryCacheError(
                "materialization fsync target 非目录")
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_private_tree(path: Path) -> None:
    if not os.path.lexists(path):
        return
    for current, dirs, _files in os.walk(path, topdown=True, followlinks=False):
        os.chmod(current, 0o700)
        for name in dirs:
            child = Path(current) / name
            if stat.S_ISLNK(os.lstat(child).st_mode):
                continue
            os.chmod(child, 0o700)
    shutil.rmtree(path)


def _strict_json(raw: bytes, *, label: str) -> Any:
    def unique(pairs):  # noqa: ANN001
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite {token}")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError,
            RecursionError) as error:
        raise RepositoryMaterializationError(
            f"{label} 不是严格 UTF-8 JSON") from error


def _bounded_string(value: Any, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value:
        raise RepositoryMaterializationError(f"{field} 须为非空字符串")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RepositoryMaterializationError(f"{field} 不是合法 UTF-8") from error
    if (len(encoded) > max_bytes
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)):
        raise RepositoryMaterializationError(f"{field} 超出文本边界")
    return value


def _safe_relpath(value: Any, *, field: str, max_depth: int) -> str:
    raw = _bounded_string(value, field=field, max_bytes=4096)
    if "\\" in raw:
        raise RepositoryMaterializationError(f"{field} 不得含反斜线")
    path = PurePosixPath(raw)
    parts = raw.split("/")
    if (path.is_absolute() or len(parts) > max_depth
            or any(part in ("", ".", "..") for part in parts)):
        raise RepositoryMaterializationError(f"{field} 非安全相对路径")
    return path.as_posix()


def _safe_component(value: Any, *, field: str) -> str:
    raw = _bounded_string(value, field=field, max_bytes=1024)
    if "/" in raw or "\\" in raw or raw in (".", ".."):
        raise RepositoryMaterializationError(f"{field} 非安全 Git tree component")
    return raw


def _positive_int(value: Any, *, field: str, maximum: Optional[int] = None) -> int:
    if (isinstance(value, bool) or not isinstance(value, int) or value <= 0
            or (maximum is not None and value > maximum)):
        raise RepositoryMaterializationError(f"{field} 须为有界正整数")
    return value


def _stable_id(namespace: str, value: Any) -> int:
    digest = hashlib.sha256(namespace.encode("ascii") + b"\0" + _canonical(value)).digest()
    # These IDs cross JSON/UI boundaries as well as SQLite.  Staying within
    # IEEE-754's exact-integer range prevents a browser/client from silently
    # rounding a protocol or metric foreign key.  Semantic collision checks
    # remain mandatory even with the 53-bit family space.
    result = int.from_bytes(digest[:8], "big") & ((1 << 53) - 1)
    return result or 1


def _git_blob_sha1(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()  # noqa: S324 - Git object identity


def _git_tree_sha1(entries: Sequence[Mapping[str, Any]]) -> str:
    chunks = []
    for entry in sorted(
            entries,
            key=lambda item: (item["name"].encode("utf-8")
                              + (b"/" if item["type"] == "tree" else b""))):
        mode = "40000" if entry["mode"] == "040000" else entry["mode"]
        chunks.append(
            mode.encode("ascii") + b" " + entry["name"].encode("utf-8")
            + b"\0" + bytes.fromhex(entry["sha"]))
    content = b"".join(chunks)
    header = f"tree {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()  # noqa: S324 - Git object identity


def _parse_lfs_pointer(payload: bytes) -> Optional[Dict[str, Any]]:
    if not payload or len(payload) >= 1024:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Git's line scanner accepts LF/CRLF and a final line without a newline.
    # Detection must be at least as broad or a valid pointer could be mistaken
    # for the model bytes that it references.
    lines = text.splitlines()
    if not lines or lines[0] != f"version {_LFS_VERSION}":
        return None
    values = {}
    for line in lines[1:]:
        if " " not in line:
            return None
        key, value = line.split(" ", 1)
        if (not re.fullmatch(r"[a-z0-9.-]+", key) or not value
                or key in values):
            return None
        values[key] = value
    oid = values.get("oid")
    size = values.get("size")
    if (not isinstance(oid, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", oid)
            or not isinstance(size, str) or not size.isascii() or not size.isdigit()):
        return None
    numeric_size = int(size)
    if numeric_size < 0:
        return None
    return {"oid": oid, "size": numeric_size}
