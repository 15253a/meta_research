"""Stable read capabilities for DB-indexed filesystem artifacts.

The database stores ``path + hash`` identities.  A consumer must not hash a
path, close it, and later let a subprocess open that path again: a rename or
symlink swap in between would detach the consumed bytes from the audited
identity.  This module opens one no-follow descriptor, verifies type,
device/inode/size and SHA-256 on that descriptor, and exposes only
``/proc/self/fd/<n>`` to a child via ``pass_fds``.

This is an identity/TOCTOU capability, not an adversarial sandbox.  A hostile
same-UID writer that mutates the already-open inode during consumption still
belongs to the stronger isolation boundary; post-use verification detects any
persistent mutation but cannot make a mutable filesystem inode cryptographically
immutable while the child runs.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Mapping, Optional


_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")


class ArtifactCapabilityError(RuntimeError):
    """An artifact path cannot prove the expected stable identity."""


def normalize_sha256(value: Any, *, field: str = "content_hash") -> str:
    if not isinstance(value, str):
        raise ArtifactCapabilityError(f"{field} 须为 sha256 hex")
    match = _SHA256_RE.fullmatch(value)
    if match is None:
        raise ArtifactCapabilityError(f"{field} 须为 sha256 hex")
    return "sha256:" + match.group(1)


def _hash_fd(fd: int) -> tuple[str, int]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as error:
        raise ArtifactCapabilityError("artifact fd 不可 seek") from error
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return "sha256:" + digest.hexdigest(), size


@dataclass(frozen=True)
class ArtifactIdentity:
    path: str
    content_hash: str
    size_bytes: int
    device: int
    inode: int


class ArtifactCapability:
    """Owned descriptor plus the exact identity verified on that descriptor."""

    def __init__(self, fd: int, identity: ArtifactIdentity):
        self.fd = fd
        self.identity = identity

    @property
    def proc_path(self) -> str:
        if self.fd < 0:
            raise ArtifactCapabilityError("artifact capability 已转移/关闭")
        return f"/proc/self/fd/{self.fd}"

    def detach(self) -> int:
        if self.fd < 0:
            raise ArtifactCapabilityError("artifact capability 已转移/关闭")
        fd, self.fd = self.fd, -1
        return fd

    def verify_unchanged(self) -> None:
        if self.fd < 0:
            raise ArtifactCapabilityError("artifact capability 已转移/关闭")
        verify_open_fd(
            self.fd, expected_hash=self.identity.content_hash,
            expected_size=self.identity.size_bytes,
            expected_device=self.identity.device,
            expected_inode=self.identity.inode)

    def verify_path_binding(self) -> None:
        """Verify that the durable path still names this exact open inode."""
        if self.fd < 0:
            raise ArtifactCapabilityError("artifact capability 已转移/关闭")
        try:
            current = os.lstat(self.identity.path)
        except OSError as error:
            raise ArtifactCapabilityError("artifact durable path 已消失") from error
        if (not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode)
                or (current.st_dev, current.st_ino, current.st_size)
                != (self.identity.device, self.identity.inode,
                    self.identity.size_bytes)):
            raise ArtifactCapabilityError("artifact durable path 不再绑定已校验 inode")

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "ArtifactCapability":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()


def open_artifact(
        path: Path | str, *, expected_hash: Optional[str] = None,
        expected_size: Optional[int] = None, label: str = "artifact"
) -> ArtifactCapability:
    """Open and hash one regular file without following the final component."""
    raw_path = os.fspath(path)
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise ArtifactCapabilityError(f"{label} path 非法")
    if (expected_size is not None and (
            isinstance(expected_size, bool) or not isinstance(expected_size, int)
            or expected_size < 0)):
        raise ArtifactCapabilityError(f"{label} expected_size 非法")
    expected = (normalize_sha256(expected_hash, field=f"{label}.content_hash")
                if expected_hash is not None else None)
    try:
        before = os.lstat(raw_path)
    except OSError as error:
        raise ArtifactCapabilityError(f"{label} 不可 lstat: {raw_path}") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ArtifactCapabilityError(f"{label} 须为非 symlink 常规文件")
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(raw_path, flags)
    except OSError as error:
        raise ArtifactCapabilityError(f"{label} 不可安全打开: {raw_path}") from error
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino, opened.st_size)
                != (before.st_dev, before.st_ino, before.st_size)):
            raise ArtifactCapabilityError(f"{label} open 前后身份漂移")
        content_hash, size = _hash_fd(fd)
        if size != opened.st_size:
            raise ArtifactCapabilityError(f"{label} 读取期间 size 漂移")
        if expected_size is not None and size != expected_size:
            raise ArtifactCapabilityError(
                f"{label} size 与权威不一致: {size} != {expected_size}")
        if expected is not None and content_hash != expected:
            raise ArtifactCapabilityError(f"{label} sha256 哈希不符（与权威不一致）")
        after = os.lstat(raw_path)
        current = os.fstat(fd)
        identity_tuple = (opened.st_dev, opened.st_ino, opened.st_size)
        if ((after.st_dev, after.st_ino, after.st_size) != identity_tuple
                or (current.st_dev, current.st_ino, current.st_size)
                != identity_tuple):
            raise ArtifactCapabilityError(f"{label} 校验期间路径/文件身份漂移")
        return ArtifactCapability(fd, ArtifactIdentity(
            path=raw_path, content_hash=content_hash, size_bytes=size,
            device=opened.st_dev, inode=opened.st_ino))
    except BaseException:
        os.close(fd)
        raise


def verify_open_fd(
        fd: int, *, expected_hash: str, expected_size: Optional[int] = None,
        expected_device: Optional[int] = None, expected_inode: Optional[int] = None
) -> ArtifactIdentity:
    """Re-hash a still-open descriptor after use and verify its inode identity."""
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
        raise ArtifactCapabilityError("artifact fd 非法")
    expected = normalize_sha256(expected_hash)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactCapabilityError("artifact fd 不再是常规文件")
    if expected_device is not None and info.st_dev != expected_device:
        raise ArtifactCapabilityError("artifact fd device 漂移")
    if expected_inode is not None and info.st_ino != expected_inode:
        raise ArtifactCapabilityError("artifact fd inode 漂移")
    content_hash, size = _hash_fd(fd)
    if expected_size is not None and size != expected_size:
        raise ArtifactCapabilityError("artifact fd size 漂移")
    if content_hash != expected:
        raise ArtifactCapabilityError("artifact fd bytes 在消费期间被改写")
    return ArtifactIdentity(
        path=f"/proc/self/fd/{fd}", content_hash=content_hash,
        size_bytes=size, device=info.st_dev, inode=info.st_ino)


def read_artifact_bytes(
        path: Path | str, *, expected_hash: Optional[str] = None,
        expected_size: Optional[int] = None, max_bytes: Optional[int] = None,
        label: str = "artifact") -> bytes:
    """Read bytes and identity from the same descriptor."""
    with open_artifact(
            path, expected_hash=expected_hash, expected_size=expected_size,
            label=label) as capability:
        size = capability.identity.size_bytes
        if (max_bytes is not None and (
                isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                or max_bytes < 0)):
            raise ArtifactCapabilityError("max_bytes 非法")
        if max_bytes is not None and size > max_bytes:
            raise ArtifactCapabilityError(f"{label} 超过 {max_bytes} bytes")
        os.lseek(capability.fd, 0, os.SEEK_SET)
        chunks = []
        remaining = size
        while remaining:
            chunk = os.read(capability.fd, min(1024 * 1024, remaining))
            if not chunk:
                raise ArtifactCapabilityError(f"{label} 读取被截断")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if ("sha256:" + hashlib.sha256(payload).hexdigest()
                != capability.identity.content_hash):
            raise ArtifactCapabilityError(
                f"{label} bytes 在校验后读取期间被改写")
        capability.verify_unchanged()
        return payload


def verify_tree_files(
        root: Path | str, expected_hashes: Mapping[str, str], *,
        label: str = "artifact tree") -> None:
    """Verify a bounded caller-provided file ledger under one stable dirfd."""
    dir_fd = open_directory(root, label=label)
    try:
        verify_tree_fd(dir_fd, expected_hashes, label=label)
    finally:
        os.close(dir_fd)


def open_directory(root: Path | str, *, label: str = "artifact tree") -> int:
    """Open a no-follow directory descriptor for `/proc/self/fd` handoff."""
    root_path = Path(root)
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        dir_fd = os.open(root_path, flags)
    except OSError as error:
        raise ArtifactCapabilityError(f"{label} 目录不可安全打开") from error
    if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
        os.close(dir_fd)
        raise ArtifactCapabilityError(f"{label} 根不是目录")
    return dir_fd


def verify_tree_fd(
        dir_fd: int, expected_hashes: Mapping[str, str], *,
        label: str = "artifact tree", exact: bool = False,
        allowed_extra: Collection[str] = ()) -> None:
    """Verify a file ledger relative to an already-open directory inode."""
    if isinstance(dir_fd, bool) or not isinstance(dir_fd, int) or dir_fd < 0:
        raise ArtifactCapabilityError(f"{label} dirfd 非法")
    try:
        if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
            raise ArtifactCapabilityError(f"{label} 根不是目录")
        for rel, expected in sorted(expected_hashes.items()):
            if (not isinstance(rel, str) or not rel or rel.startswith("/")
                    or "\\" in rel or any(part in ("", ".", "..")
                                           for part in rel.split("/"))):
                raise ArtifactCapabilityError(f"{label} ledger 路径非法: {rel!r}")
            # /proc anchors the already-open root inode.  O_NOFOLLOW still
            # rejects a symlink leaf; nested-dir adversarial mutation remains
            # part of the stronger sandbox boundary documented above.
            path = Path(f"/proc/self/fd/{dir_fd}") / rel
            with open_artifact(
                    path, expected_hash=expected,
                    label=f"{label}:{rel}"):
                pass
        if exact:
            disk = set()
            root = Path(f"/proc/self/fd/{dir_fd}")
            for current, dirs, files in os.walk(root, followlinks=False):
                for name in dirs:
                    entry = Path(current) / name
                    if entry.is_symlink():
                        raise ArtifactCapabilityError(
                            f"{label} 出现 symlink 目录")
                for name in files:
                    disk.add(str((Path(current) / name).relative_to(root)))
            extras = disk - set(expected_hashes) - set(allowed_extra)
            missing_extra = set(allowed_extra) - disk
            if extras or missing_extra:
                raise ArtifactCapabilityError(
                    f"{label} 文件闭包漂移: extra={sorted(extras)}, "
                    f"missing_reserved={sorted(missing_extra)}")
    except OSError as error:
        raise ArtifactCapabilityError(f"{label} dirfd 不可用") from error
