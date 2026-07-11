"""Console control-plane filesystem capabilities.

This module deliberately keeps the HTTP process' writable surface smaller than
``work_root``:

* :class:`ConsoleSpool` appends only to ``state/console_inbox.jsonl`` while a
  stable, never-unlinked lock below ``work_root`` is held.  Every opened object
  is checked through its file descriptor; symlinks, hardlinks and non-regular
  files are rejected.
* :func:`open_pinned_upload_ref` walks an upload reference with ``openat`` and
  ``O_NOFOLLOW``.  The returned ``/proc/self/fd/<n>`` path is a capability for
  the already-open directory, not a pathname that can be retargeted after a
  containment check.
* :func:`read_regular_file_beneath` opens and reads a bounded regular file from
  the same descriptor, closing the usual ``resolve/is_file/read`` TOCTOU gap.

Linux is already required by the surrounding implementation (``fcntl.flock``
and ``/proc`` are used intentionally).  No caller should unlink the spool lock.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union


_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
              | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
              | getattr(os, "O_NONBLOCK", 0))
_FILE_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
_FILE_RW_FLAGS = (os.O_RDWR | os.O_APPEND | os.O_CREAT
                  | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                  | getattr(os, "O_NONBLOCK", 0))
_FILE_CREATE_FLAGS = (os.O_RDWR | os.O_CREAT
                      | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                      | getattr(os, "O_NONBLOCK", 0))
_FILE_EXISTING_FLAGS = (os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
_LOCK_NAME = ".console-inbox.lock"
_STATE_NAME = "state"
_INBOX_NAME = "console_inbox.jsonl"
CAPABILITY_NAME = ".console-capability"
_CURSOR_NAME = "console_inbox.cursor"
_RETRY_NAME = ".console_inbox.retry.json"
_MAX_SOURCE_REF_CHARS = 4_096
MAX_INBOX_BYTES = 256 * 1024 * 1024
MAX_RECORD_BYTES = 128 * 1024
MAX_BATCH_BYTES = 4 * 1024 * 1024
_MAX_SIDECAR_BYTES = 64 * 1024
_CURSOR_ANCHOR_BYTES = 4 * 1024


class UnsafeConsolePath(OSError):
    """A control-plane path failed an inode/type/containment check."""


@dataclass(frozen=True)
class SpoolRecord:
    end_offset: int
    line: Optional[str]
    error: Optional[str] = None
    anchor: str = ""


@dataclass(frozen=True)
class SpoolBatch:
    inbox_dev: int
    inbox_ino: int
    start_offset: int
    records: Tuple[SpoolRecord, ...]
    start_anchor: str = ""
    has_more_committed: bool = False


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _verify_directory_fd(fd: int, *, label: str) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeConsolePath(f"{label} 不是实体目录")
    return info


def _verify_regular_fd(fd: int, *, label: str) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeConsolePath(f"{label} 不是常规文件（拒绝 FIFO/device/socket）")
    if info.st_nlink != 1:
        raise UnsafeConsolePath(f"{label} 必须只有一个硬链接")
    return info


def _verify_entry_matches_fd(parent_fd: int, name: str, fd: int, *, label: str,
                             regular: bool) -> os.stat_result:
    """Verify that ``name`` still denotes the descriptor opened beneath parent."""
    opened = _verify_regular_fd(fd, label=label) if regular else _verify_directory_fd(fd, label=label)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise UnsafeConsolePath(f"{label} 在打开后被替换") from error
    if not _same_inode(opened, current):
        raise UnsafeConsolePath(f"{label} 在打开后被替换")
    if regular:
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise UnsafeConsolePath(f"{label} 路径项不是独占常规文件")
    elif not stat.S_ISDIR(current.st_mode):
        raise UnsafeConsolePath(f"{label} 路径项不是实体目录")
    return opened


def open_directory_path(path: Union[str, Path], *, label: str) -> int:
    """Open every component of an absolute path with ``openat/O_NOFOLLOW``.

    Resolving first and opening later would simply move the race.  Starting at
    ``/`` and retaining each directory descriptor makes every next lookup
    relative to an already-open, trusted inode.
    """
    absolute = os.path.abspath(os.fspath(path))
    parts = [part for part in absolute.split(os.sep) if part]
    fd = os.open(os.sep, _DIR_FLAGS)
    try:
        _verify_directory_fd(fd, label="filesystem root")
        for part in parts:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            try:
                _verify_directory_fd(next_fd, label=label)
            except BaseException:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_directory_at(parent_fd: int, name: str, *, label: str) -> int:
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        _verify_entry_matches_fd(parent_fd, name, fd, label=label, regular=False)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_directory_components(parent_fd: int, components: Iterable[str], *, label: str) -> int:
    fd = os.dup(parent_fd)
    try:
        for component in components:
            next_fd = _open_directory_at(fd, component, label=label)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("console spool append 未写入任何字节")
        view = view[written:]


def _read_all(fd: int, *, max_bytes: Optional[int] = None) -> bytes:
    chunks = []
    total = 0
    while True:
        limit = 1024 * 1024
        if max_bytes is not None:
            limit = min(limit, max_bytes + 1 - total)
            if limit <= 0:
                raise ValueError(f"文件超过读取上限 {max_bytes} 字节")
        chunk = os.read(fd, limit)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise ValueError(f"文件超过读取上限 {max_bytes} 字节")


def _open_exclusive_or_existing(parent_fd: int, name: str, flags: int, mode: int) -> Tuple[int, bool]:
    try:
        return os.open(name, flags | os.O_EXCL, mode, dir_fd=parent_fd), True
    except FileExistsError:
        return os.open(name, flags, mode, dir_fd=parent_fd), False


def _open_optional_private_regular(parent_fd: int, name: str, *, label: str) -> Optional[int]:
    try:
        fd = os.open(name, _FILE_EXISTING_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    try:
        info = _verify_entry_matches_fd(parent_fd, name, fd, label=label, regular=True)
        if info.st_uid != os.geteuid():
            raise UnsafeConsolePath(f"{label} owner 不是当前 uid")
        if stat.S_IMODE(info.st_mode) != 0o600:
            # Cursor/inbox files from older releases used process umask.  They
            # are safe to migrate only after inode/owner/link checks succeed.
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            os.fsync(parent_fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _atomic_write_private_sidecar(parent_fd: int, name: str, payload: bytes, *, label: str) -> None:
    """Durably replace a small 0600 sidecar after rejecting unsafe old entries."""
    if len(payload) > _MAX_SIDECAR_BYTES:
        raise ValueError(f"{label} 超过 {_MAX_SIDECAR_BYTES} 字节上限")
    old_fd = _open_optional_private_regular(parent_fd, name, label=label)
    old_info = os.fstat(old_fd) if old_fd is not None else None
    if old_fd is not None:
        os.close(old_fd)
    temp_name = f".{name}.tmp-{secrets.token_hex(16)}"
    temp_fd = -1
    installed = False
    try:
        temp_fd = os.open(
            temp_name, _FILE_CREATE_FLAGS | os.O_EXCL, 0o600, dir_fd=parent_fd)
        info = _verify_entry_matches_fd(
            parent_fd, temp_name, temp_fd, label=f"{label} temp", regular=True)
        if info.st_uid != os.geteuid():
            raise UnsafeConsolePath(f"{label} temp owner 不是当前 uid")
        os.fchmod(temp_fd, 0o600)
        os.ftruncate(temp_fd, 0)
        os.lseek(temp_fd, 0, os.SEEK_SET)
        _write_all(temp_fd, payload)
        os.fsync(temp_fd)
        if old_info is not None:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (not _same_inode(old_info, current) or not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1 or current.st_uid != os.geteuid()):
                raise UnsafeConsolePath(f"{label} 在替换前发生变化")
        else:
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise UnsafeConsolePath(f"{label} 在创建前被抢占")
        os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        installed = True
        os.fsync(parent_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if not installed:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _ensure_private_state(work_fd: int) -> Tuple[int, bool]:
    created = False
    try:
        state_fd = _open_directory_at(work_fd, _STATE_NAME, label="console state")
    except FileNotFoundError:
        try:
            os.mkdir(_STATE_NAME, mode=0o700, dir_fd=work_fd)
            created = True
        except FileExistsError:
            # Another process may have won creation before the stable lock was
            # installed on a fresh work root.  The descriptor checks below are
            # still authoritative.
            pass
        state_fd = _open_directory_at(work_fd, _STATE_NAME, label="console state")
    try:
        state_info = os.fstat(state_fd)
        if state_info.st_uid != os.geteuid():
            raise UnsafeConsolePath("console state owner 不是当前 uid")
        mode = stat.S_IMODE(state_info.st_mode)
        if mode != 0o700:
            os.fchmod(state_fd, 0o700)
        os.fsync(state_fd)
        if created:
            os.fsync(work_fd)
        return state_fd, created
    except BaseException:
        os.close(state_fd)
        raise


class DurableInboxSpool:
    """Cross-thread/process durable append-only inbox with an isolated namespace.

    ``ConsoleSpool`` and authenticated connector ingress deliberately use
    different files, locks, cursors and retry state.  They share only these
    fd-level durability mechanics; a remote connector can therefore never
    manufacture a console control record or head-of-line block the emergency
    console queue.
    """

    def __init__(self, work_root: Union[str, Path], *, lock_name: str,
                 inbox_name: str, cursor_name: str, retry_name: str,
                 key_prefix: str, label: str, capability_name: Optional[str] = None,
                 fence_uncommitted_tail: bool = False):
        self.work_root = Path(work_root)
        names = (lock_name, inbox_name, cursor_name, retry_name)
        if (any(not isinstance(name, str) or not name or "/" in name or "\x00" in name
                or name in (".", "..") for name in names)
                or not isinstance(key_prefix, str)
                or not key_prefix
                or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in key_prefix)
                or not isinstance(label, str) or not label
                or not isinstance(fence_uncommitted_tail, bool)):
            raise ValueError("durable inbox namespace 非法")
        self._lock_name = lock_name
        self._inbox_name = inbox_name
        self._cursor_name = cursor_name
        self._retry_name = retry_name
        self._key_prefix = key_prefix
        self._label = label
        self._capability_name = capability_name
        self._fence_uncommitted_tail = fence_uncommitted_tail
        self.inbox_path = self.work_root / _STATE_NAME / self._inbox_name
        self._thread_lock = threading.Lock()

    def _open_lock(self, work_fd: int) -> Tuple[int, bool]:
        lock_fd, created = _open_exclusive_or_existing(
            work_fd, self._lock_name, _FILE_RW_FLAGS, 0o600)
        try:
            info = _verify_entry_matches_fd(
                work_fd, self._lock_name, lock_fd,
                label=f"{self._label} spool claim", regular=True)
            if info.st_uid != os.geteuid():
                raise UnsafeConsolePath(f"{self._label} spool claim owner 不是当前 uid")
            os.fchmod(lock_fd, 0o600)
            if created:
                os.fsync(lock_fd)
                os.fsync(work_fd)
            return lock_fd, created
        except BaseException:
            os.close(lock_fd)
            raise

    @contextmanager
    def _claimed_state(self):
        """Yield ``(work_fd, state_fd)`` under the stable cross-process claim."""
        with self._thread_lock:
            work_fd = open_directory_path(self.work_root, label="work_root")
            lock_fd = -1
            state_fd = -1
            acquired = False
            try:
                lock_fd, _ = self._open_lock(work_fd)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                acquired = True
                _verify_entry_matches_fd(
                    work_fd, self._lock_name, lock_fd,
                    label=f"{self._label} spool claim", regular=True)
                state_fd, _ = _ensure_private_state(work_fd)
                yield work_fd, state_fd
            finally:
                if state_fd >= 0:
                    os.close(state_fd)
                if acquired:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
                elif lock_fd >= 0:
                    os.close(lock_fd)
                os.close(work_fd)

    @staticmethod
    def _cursor_anchor(inbox_fd: int, offset: int) -> str:
        start = max(0, offset - _CURSOR_ANCHOR_BYTES)
        remaining = offset - start
        chunks = []
        position = start
        while remaining:
            chunk = os.pread(inbox_fd, remaining, position)
            if not chunk:
                raise UnsafeConsolePath("console cursor anchor 超出 inbox")
            chunks.append(chunk)
            position += len(chunk)
            remaining -= len(chunk)
        return hashlib.sha256(b"".join(chunks)).hexdigest()

    @staticmethod
    def _legacy_line_cursor(inbox_fd: int, inbox_size: int, line_count: int) -> int:
        if line_count <= 0:
            return 0
        position = 0
        seen = 0
        while position < inbox_size:
            chunk = os.pread(inbox_fd, min(1024 * 1024, inbox_size - position), position)
            if not chunk:
                break
            search_from = 0
            while True:
                index = chunk.find(b"\n", search_from)
                if index < 0:
                    break
                seen += 1
                if seen == line_count:
                    return position + index + 1
                search_from = index + 1
            position += len(chunk)
        # Old behavior reset an out-of-range line cursor and replayed through
        # DB idempotency, which is safer than skipping an unknown prefix.
        return 0

    def _load_cursor_locked(self, state_fd: int, inbox_fd: int,
                            inbox_info: os.stat_result) -> Tuple[int, bool]:
        cursor_fd = _open_optional_private_regular(
            state_fd, self._cursor_name, label=f"{self._label} inbox cursor")
        if cursor_fd is None:
            return 0, False
        try:
            info = os.fstat(cursor_fd)
            if info.st_size > _MAX_SIDECAR_BYTES:
                return 0, False
            os.lseek(cursor_fd, 0, os.SEEK_SET)
            raw = _read_all(cursor_fd, max_bytes=_MAX_SIDECAR_BYTES)
        finally:
            os.close(cursor_fd)
        try:
            decoded = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return 0, False
        if not decoded:
            return 0, False
        if not decoded.startswith("{"):
            try:
                return self._legacy_line_cursor(
                    inbox_fd, inbox_info.st_size, int(decoded)), True
            except ValueError:
                return 0, True
        try:
            cursor = json.loads(decoded)
            offset = cursor["offset"]
            if (set(cursor) != {"version", "offset", "inbox_dev", "inbox_ino", "anchor"}
                    or cursor["version"] != 1 or isinstance(offset, bool)
                    or not isinstance(offset, int) or offset < 0
                    or cursor["inbox_dev"] != inbox_info.st_dev
                    or cursor["inbox_ino"] != inbox_info.st_ino
                    or offset > inbox_info.st_size):
                return 0, False
            if offset and os.pread(inbox_fd, 1, offset - 1) != b"\n":
                return 0, False
            if cursor["anchor"] != self._cursor_anchor(inbox_fd, offset):
                return 0, False
            return offset, False
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            return 0, False

    def _write_cursor_locked(self, state_fd: int, inbox_fd: int,
                             inbox_info: os.stat_result, offset: int) -> None:
        payload = json.dumps({
            "version": 1,
            "offset": offset,
            "inbox_dev": inbox_info.st_dev,
            "inbox_ino": inbox_info.st_ino,
            "anchor": self._cursor_anchor(inbox_fd, offset),
        }, sort_keys=True, separators=(",", ":")).encode("ascii")
        _atomic_write_private_sidecar(
            state_fd, self._cursor_name, payload, label=f"{self._label} inbox cursor")

    @staticmethod
    def _next_record(inbox_fd: int, start: int, inbox_size: int) -> Optional[SpoolRecord]:
        position = start
        captured = bytearray()
        oversized = False
        while position < inbox_size:
            chunk = os.pread(inbox_fd, min(64 * 1024, inbox_size - position), position)
            if not chunk:
                return None
            newline = chunk.find(b"\n")
            segment = chunk if newline < 0 else chunk[:newline]
            if not oversized:
                if len(captured) + len(segment) > MAX_RECORD_BYTES:
                    captured.clear()
                    oversized = True
                else:
                    captured.extend(segment)
            if newline >= 0:
                end = position + newline + 1
                if oversized:
                    return SpoolRecord(
                        end_offset=end, line=None,
                        error=f"committed record 超过 {MAX_RECORD_BYTES} 字节上限")
                try:
                    line = bytes(captured).decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    return SpoolRecord(
                        end_offset=end, line=None,
                        error="committed record 不是严格 UTF-8")
                return SpoolRecord(end_offset=end, line=line)
            position += len(chunk)
        # EOF without LF is an uncommitted crash tail.  It remains invisible
        # until a producer fences it or an operator repairs the spool.
        return None

    def read_pending(self) -> SpoolBatch:
        """Read one bounded batch after the durable byte-offset cursor.

        A committed oversized record is returned with ``line=None`` and an
        ``end_offset`` so the consumer can record it as poison and advance;
        malformed input therefore cannot wedge all later actions.
        """
        with self._claimed_state() as (_work_fd, state_fd):
            inbox_fd = _open_optional_private_regular(
                state_fd, self._inbox_name, label=f"{self._label} inbox")
            if inbox_fd is None:
                return SpoolBatch(0, 0, 0, ())
            try:
                info = os.fstat(inbox_fd)
                if info.st_size > MAX_INBOX_BYTES:
                    raise UnsafeConsolePath(
                        f"console inbox 超过 {MAX_INBOX_BYTES} 字节安全上限")
                start, legacy_migration = self._load_cursor_locked(state_fd, inbox_fd, info)
                if legacy_migration:
                    # Persist even when no pending record follows.  Otherwise a
                    # legacy line-count cursor at EOF would rescan the full
                    # historical spool on every precheck forever.
                    self._write_cursor_locked(state_fd, inbox_fd, info, start)
                records: List[SpoolRecord] = []
                start_anchor = self._cursor_anchor(inbox_fd, start)
                position = start
                hit_batch_limit = False
                while position < info.st_size:
                    record = self._next_record(inbox_fd, position, info.st_size)
                    if record is None:
                        break
                    record = SpoolRecord(
                        record.end_offset, record.line, record.error,
                        self._cursor_anchor(inbox_fd, record.end_offset))
                    records.append(record)
                    position = record.end_offset
                    if position - start >= MAX_BATCH_BYTES:
                        hit_batch_limit = True
                        break
                # Only a following LF-committed record is backlog.  An EOF
                # crash tail remains invisible and must not keep run blocked.
                has_more = (hit_batch_limit
                            and self._next_record(inbox_fd, position, info.st_size) is not None)
                return SpoolBatch(
                    info.st_dev, info.st_ino, start, tuple(records), start_anchor, has_more)
            finally:
                os.close(inbox_fd)

    def write_cursor(self, batch: SpoolBatch, offset: int) -> None:
        """Persist a processed byte offset only for the inbox inode read."""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("console cursor offset 非法")
        allowed_offsets = {batch.start_offset}
        allowed_offsets.update(record.end_offset for record in batch.records)
        if offset not in allowed_offsets:
            raise ValueError("console cursor 只能落在本批起点或 record end_offset")
        with self._claimed_state() as (_work_fd, state_fd):
            inbox_fd = _open_optional_private_regular(
                state_fd, self._inbox_name, label=f"{self._label} inbox")
            if inbox_fd is None:
                if offset == 0:
                    return
                raise UnsafeConsolePath("console inbox 已消失，拒绝写入旧 cursor")
            try:
                info = os.fstat(inbox_fd)
                if (info.st_dev, info.st_ino) != (batch.inbox_dev, batch.inbox_ino):
                    raise UnsafeConsolePath("console inbox inode 已替换，拒绝写入旧 cursor")
                current_offset, legacy_migration = self._load_cursor_locked(
                    state_fd, inbox_fd, info)
                if legacy_migration:
                    # Normalize the authoritative legacy value before applying
                    # the compare-and-swap below.
                    self._write_cursor_locked(
                        state_fd, inbox_fd, info, current_offset)
                batch_offsets = [batch.start_offset]
                batch_offsets.extend(record.end_offset for record in batch.records)
                offset_ordinals = {
                    batch_offset: ordinal
                    for ordinal, batch_offset in enumerate(batch_offsets)
                }
                if (len(offset_ordinals) != len(batch_offsets)
                        or any(right <= left for left, right in zip(
                            batch_offsets, batch_offsets[1:]))):
                    raise UnsafeConsolePath(
                        "console batch record 边界非严格递增，拒绝 cursor commit")
                if current_offset not in offset_ordinals:
                    raise UnsafeConsolePath(
                        "console cursor 已被其他 consumer 推进到本批之外，拒绝旧 batch commit")
                if offset not in offset_ordinals:
                    raise UnsafeConsolePath("console cursor 不属于本批 committed record 边界")
                if offset > info.st_size or (offset and os.pread(inbox_fd, 1, offset - 1) != b"\n"):
                    raise UnsafeConsolePath("console cursor 不在 committed record 边界")
                anchors = {batch.start_offset: batch.start_anchor}
                anchors.update((record.end_offset, record.anchor) for record in batch.records)
                # Verify both sides of the CAS against the generation that was
                # read.  This rejects same-inode truncate/regrow ABA even for
                # an otherwise idempotent duplicate commit.
                if (anchors[current_offset] != self._cursor_anchor(inbox_fd, current_offset)
                        or anchors[offset] != self._cursor_anchor(inbox_fd, offset)):
                    raise UnsafeConsolePath("console inbox 内容在读取后发生变化，拒绝推进 cursor")
                if offset_ordinals[offset] < offset_ordinals[current_offset]:
                    raise UnsafeConsolePath(
                        "console cursor 已被其他 consumer 推进，拒绝旧 batch 回退 durable cursor")
                if current_offset == offset:
                    return                       # verified duplicate commit is idempotent
                self._write_cursor_locked(state_fd, inbox_fd, info, offset)
            finally:
                os.close(inbox_fd)

    def load_retry_counts(self) -> Dict[str, int]:
        """Load durable retry counters; corrupt state fails closed."""
        with self._claimed_state() as (_work_fd, state_fd):
            retry_fd = _open_optional_private_regular(
                state_fd, self._retry_name, label=f"{self._label} inbox retry state")
            if retry_fd is None:
                return {}
            try:
                info = os.fstat(retry_fd)
                if info.st_size > _MAX_SIDECAR_BYTES:
                    raise ValueError("console inbox retry state 过大")
                os.lseek(retry_fd, 0, os.SEEK_SET)
                raw = _read_all(retry_fd, max_bytes=_MAX_SIDECAR_BYTES)
            finally:
                os.close(retry_fd)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("console inbox retry state 损坏") from error
        if not isinstance(value, dict):
            raise ValueError("console inbox retry state 须为 object")
        clean: Dict[str, int] = {}
        for key, count in value.items():
            if (not isinstance(key, str) or not key or len(key) > 256
                    or isinstance(count, bool) or not isinstance(count, int)
                    or count < 0 or count > 1_000_000):
                raise ValueError("console inbox retry state 条目损坏")
            clean[key] = count
        return clean

    def store_retry_counts(self, counts: Dict[str, int]) -> None:
        """Atomically persist validated retry counters below the stable claim."""
        if not isinstance(counts, dict):
            raise ValueError("console inbox retry state 须为 object")
        clean: Dict[str, int] = {}
        for key, count in counts.items():
            if (not isinstance(key, str) or not key or len(key) > 256
                    or isinstance(count, bool) or not isinstance(count, int)
                    or count < 0 or count > 1_000_000):
                raise ValueError("console inbox retry state 条目损坏")
            clean[key] = count
        payload = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with self._claimed_state() as (_work_fd, state_fd):
            _atomic_write_private_sidecar(
                state_fd, self._retry_name, payload,
                label=f"{self._label} inbox retry state")

    @staticmethod
    def _validate_capability_token(token: str) -> str:
        if (not isinstance(token, str) or len(token) != 64
                or any(ch not in "0123456789abcdef" for ch in token)):
            raise ValueError("console capability 须为 256-bit 小写 hex（64 字符）")
        return token

    def load_or_create_capability(self, explicit_token: Optional[str] = None) -> str:
        """Load the persistent bearer capability, creating it durably once.

        Tests may pass a fixed 256-bit token.  Production passes ``None`` and
        receives a cryptographically random token.  An existing file is never
        silently repaired: owner/mode/content drift is a fail-closed condition.
        """
        if self._capability_name is None:
            raise ValueError(f"{self._label} inbox 不提供 HTTP capability")
        if explicit_token is not None:
            explicit_token = self._validate_capability_token(explicit_token)
        with self._thread_lock:
            work_fd = open_directory_path(self.work_root, label="work_root")
            lock_fd = -1
            state_fd = -1
            capability_fd = -1
            acquired = False
            try:
                lock_fd, _ = self._open_lock(work_fd)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                acquired = True
                _verify_entry_matches_fd(
                    work_fd, self._lock_name, lock_fd,
                    label=f"{self._label} spool claim", regular=True)
                state_fd, _ = _ensure_private_state(work_fd)
                capability_fd, created = _open_exclusive_or_existing(
                    state_fd, self._capability_name, _FILE_CREATE_FLAGS, 0o600)
                info = _verify_entry_matches_fd(
                    state_fd, self._capability_name, capability_fd,
                    label="console bearer capability", regular=True)
                if info.st_uid != os.geteuid():
                    raise UnsafeConsolePath("console bearer capability owner 不是当前 uid")
                if created:
                    os.fchmod(capability_fd, 0o600)
                    token = explicit_token or secrets.token_hex(32)
                    os.lseek(capability_fd, 0, os.SEEK_SET)
                    _write_all(capability_fd, token.encode("ascii"))
                    os.fsync(capability_fd)
                    os.fsync(state_fd)
                    _verify_entry_matches_fd(
                        state_fd, self._capability_name, capability_fd,
                        label="console bearer capability", regular=True)
                else:
                    if stat.S_IMODE(info.st_mode) != 0o600:
                        raise UnsafeConsolePath("console bearer capability mode 必须恰为 0600")
                    if info.st_size != 64:
                        raise UnsafeConsolePath("console bearer capability 长度损坏")
                    os.lseek(capability_fd, 0, os.SEEK_SET)
                    try:
                        token = _read_all(capability_fd, max_bytes=64).decode("ascii")
                    except (UnicodeDecodeError, ValueError) as error:
                        raise UnsafeConsolePath("console bearer capability 内容损坏") from error
                    token = self._validate_capability_token(token)
                    if explicit_token is not None and token != explicit_token:
                        raise UnsafeConsolePath("显式 console capability 与持久文件不一致")
                return token
            finally:
                if capability_fd >= 0:
                    os.close(capability_fd)
                if acquired:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
                elif lock_fd >= 0:
                    os.close(lock_fd)
                if state_fd >= 0:
                    os.close(state_fd)
                os.close(work_fd)

    def append(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """Append one committed JSON record and return its stored shape.

        ``seq`` remains a human display cursor.  Correctness/idempotency is
        anchored by an HTTP caller's validated 128-bit nonce, or a fresh
        random nonce for direct in-process callers, so spool repair cannot
        resurrect an old sequential ``console-1`` identity.
        """
        with self._thread_lock:
            work_fd = open_directory_path(self.work_root, label="work_root")
            lock_fd = -1
            state_fd = -1
            inbox_fd = -1
            acquired = False
            try:
                lock_fd, _ = self._open_lock(work_fd)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                acquired = True
                # Re-check after waiting: a replaced/unlinked inode must never
                # become a split-brain claim.
                _verify_entry_matches_fd(
                    work_fd, self._lock_name, lock_fd,
                    label=f"{self._label} spool claim", regular=True)

                state_fd, _ = _ensure_private_state(work_fd)
                inbox_fd, created = _open_exclusive_or_existing(
                    state_fd, self._inbox_name, _FILE_RW_FLAGS, 0o600)
                inbox_info = _verify_entry_matches_fd(
                    state_fd, self._inbox_name, inbox_fd,
                    label=f"{self._label} inbox", regular=True)
                if inbox_info.st_uid != os.geteuid():
                    raise UnsafeConsolePath("console inbox owner 不是当前 uid")
                os.fchmod(inbox_fd, 0o600)

                inbox_info = os.fstat(inbox_fd)
                size = inbox_info.st_size
                if size > MAX_INBOX_BYTES:
                    raise UnsafeConsolePath(
                        f"console inbox 已超过 {MAX_INBOX_BYTES} 字节安全上限，须人工归档")
                if size and os.pread(inbox_fd, 1, size - 1) != b"\n":
                    if not self._fence_uncommitted_tail:
                        # Connector transport ACK means the whole durable log
                        # remains consumable.  Turning an old partial write
                        # into a committed poison line immediately before ACK
                        # would violate that contract.  Startup owns safe
                        # hash-only audit + truncation instead.
                        raise UnsafeConsolePath(
                            f"{self._label} inbox 含未提交尾部；须重启执行恢复")
                    # Browser console keeps its historical poison-line
                    # behavior: no remote transport ACK depends on this log.
                    _write_all(inbox_fd, b"\n")
                    size += 1

                stored = dict(rec)
                supplied_key = stored.get("idempotency_key")
                if supplied_key is None:
                    supplied_key = f"{self._key_prefix}-{secrets.token_hex(16)}"
                prefix = self._key_prefix + "-"
                if (not isinstance(supplied_key, str)
                        or len(supplied_key) != len(prefix) + 32
                        or not supplied_key.startswith(prefix)
                        or any(ch not in "0123456789abcdef" for ch in supplied_key[len(prefix):])):
                    raise ValueError(
                        f"{self._label} spool idempotency_key 须为 {prefix} 加 128-bit 小写 hex")
                stored.update({
                    # Human display cursor only: one-based byte position at
                    # which this JSON record begins.  It is monotonic and O(1)
                    # to allocate; DB idempotency uses the random key below.
                    "seq": size + 1,
                    "idempotency_key": supplied_key,
                })
                payload = (json.dumps(stored, ensure_ascii=False) + "\n").encode("utf-8")
                if size + len(payload) > MAX_INBOX_BYTES:
                    raise UnsafeConsolePath(
                        f"console inbox 将超过 {MAX_INBOX_BYTES} 字节安全上限，须人工归档")
                _write_all(inbox_fd, payload)
                os.fsync(inbox_fd)
                # Required when this append created the directory entry.  It
                # is harmless and cheap enough to keep unconditional, and also
                # persists a concurrent metadata repair (0600).
                os.fsync(state_fd)
                if created:
                    os.fsync(work_fd)
                _verify_entry_matches_fd(
                    state_fd, self._inbox_name, inbox_fd,
                    label=f"{self._label} inbox", regular=True)
                return stored
            finally:
                if inbox_fd >= 0:
                    os.close(inbox_fd)
                if acquired:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
                elif lock_fd >= 0:
                    os.close(lock_fd)
                if state_fd >= 0:
                    os.close(state_fd)
                os.close(work_fd)

    def scan_committed(self, *, max_records: int = 100_000) -> List[Dict[str, Any]]:
        """Return every LF-committed JSON object, independent of the consumer cursor.

        Authenticated ingress uses this once at startup to reconstruct its
        acceptance/idempotency index.  A duplicate provider delivery can then
        receive the same transport ACK without appending another physical
        record.  Corrupt history fails loud; it is never silently forgotten.
        """
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
            raise ValueError("scan_committed max_records 须为正整数")
        with self._claimed_state() as (_work_fd, state_fd):
            inbox_fd = _open_optional_private_regular(
                state_fd, self._inbox_name, label=f"{self._label} inbox")
            if inbox_fd is None:
                return []
            try:
                info = os.fstat(inbox_fd)
                if info.st_size > MAX_INBOX_BYTES:
                    raise UnsafeConsolePath(
                        f"{self._label} inbox 超过 {MAX_INBOX_BYTES} 字节安全上限")
                position = 0
                records: List[Dict[str, Any]] = []
                while position < info.st_size:
                    record = self._next_record(inbox_fd, position, info.st_size)
                    if record is None:
                        break
                    if record.line is None:
                        raise UnsafeConsolePath(
                            f"{self._label} inbox 历史含不可恢复 committed record: {record.error}")
                    def unique_object(pairs):  # noqa: ANN001
                        value = {}
                        for key, item in pairs:
                            if key in value:
                                raise ValueError(f"重复 JSON key: {key}")
                            value[key] = item
                        return value

                    try:
                        value = json.loads(
                            record.line, object_pairs_hook=unique_object,
                            parse_constant=lambda token: (_ for _ in ()).throw(
                                ValueError(f"非有限 JSON number: {token}")))
                    except (json.JSONDecodeError, ValueError) as error:
                        raise UnsafeConsolePath(
                            f"{self._label} inbox 历史含坏 JSON") from error
                    if not isinstance(value, dict):
                        raise UnsafeConsolePath(f"{self._label} inbox 历史记录须为 object")
                    records.append(value)
                    if len(records) > max_records:
                        raise UnsafeConsolePath(
                            f"{self._label} inbox 记录数超过 {max_records}，须人工归档")
                    position = record.end_offset
                if position != info.st_size and not self._fence_uncommitted_tail:
                    raise UnsafeConsolePath(
                        f"{self._label} inbox 含未提交尾部；拒绝建立 acceptance index")
                return records
            finally:
                os.close(inbox_fd)

    def append_audit_record(self, name: str, record: Dict[str, Any], *,
                            max_bytes: int = 16 * 1024 * 1024) -> None:
        """Durably append a bounded, non-consumable security audit record."""
        if (not isinstance(name, str) or not name or "/" in name or "\x00" in name
                or name in (".", "..")):
            raise ValueError("audit record 文件名非法")
        if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1024):
            raise ValueError("audit record 大小上限非法")
        if not isinstance(record, dict):
            raise ValueError("audit record 须为 object")
        payload = (json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > MAX_RECORD_BYTES:
            raise ValueError("audit record 超过单条上限")
        with self._claimed_state() as (_work_fd, state_fd):
            fd, _created = _open_exclusive_or_existing(
                state_fd, name, _FILE_RW_FLAGS, 0o600)
            try:
                info = _verify_entry_matches_fd(
                    state_fd, name, fd, label=f"{self._label} audit", regular=True)
                if info.st_uid != os.geteuid():
                    raise UnsafeConsolePath(f"{self._label} audit owner 不是当前 uid")
                os.fchmod(fd, 0o600)
                size = os.fstat(fd).st_size
                if size and os.pread(fd, 1, size - 1) != b"\n":
                    _write_all(fd, b"\n")
                    size += 1
                if size + len(payload) > max_bytes:
                    raise UnsafeConsolePath(
                        f"{self._label} audit 将超过 {max_bytes} 字节上限")
                _write_all(fd, payload)
                os.fsync(fd)
                os.fsync(state_fd)
                _verify_entry_matches_fd(
                    state_fd, name, fd, label=f"{self._label} audit", regular=True)
            finally:
                os.close(fd)

    def scan_audit_records(self, name: str, *, max_bytes: int = 16 * 1024 * 1024,
                           max_records: int = 100_000) -> List[Dict[str, Any]]:
        """Strictly read a security log; any torn/corrupt evidence fails loud."""
        if (not isinstance(name, str) or not name or "/" in name or "\x00" in name
                or name in (".", "..")):
            raise ValueError("audit record 文件名非法")
        if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1024
                or isinstance(max_records, bool) or not isinstance(max_records, int)
                or max_records < 1):
            raise ValueError("audit record 读取上限非法")
        with self._claimed_state() as (_work_fd, state_fd):
            fd = _open_optional_private_regular(
                state_fd, name, label=f"{self._label} audit")
            if fd is None:
                return []
            try:
                raw = _read_all(fd, max_bytes=max_bytes)
            finally:
                os.close(fd)
        if raw and not raw.endswith(b"\n"):
            raise UnsafeConsolePath(f"{self._label} audit 含未提交尾部")
        records: List[Dict[str, Any]] = []

        def unique_object(pairs):  # noqa: ANN001
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError(f"重复 JSON key: {key}")
                value[key] = item
            return value

        for line in raw.split(b"\n")[:-1]:
            if not line:
                continue
            try:
                value = json.loads(
                    line.decode("utf-8"), object_pairs_hook=unique_object,
                    parse_constant=lambda token: (_ for _ in ()).throw(
                        ValueError(f"非有限 JSON number: {token}")))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise UnsafeConsolePath(f"{self._label} audit 历史损坏") from error
            if not isinstance(value, dict):
                raise UnsafeConsolePath(f"{self._label} audit 历史记录须为 object")
            records.append(value)
            if len(records) > max_records:
                raise UnsafeConsolePath(
                    f"{self._label} audit 记录数超过 {max_records}")
        return records

    def repair_uncommitted_tail(self, audit_name: str, *,
                                max_audit_bytes: int = 16 * 1024 * 1024
                                ) -> Optional[Dict[str, Any]]:
        """Durably audit and truncate bytes after the final committed LF.

        Connector append ACKs only LF-committed records.  Therefore a crash
        tail has never been acknowledged and may be discarded, but only after
        preserving a hash/length/offset record.  No provider text is copied to
        the audit log.
        """
        if self._fence_uncommitted_tail:
            raise ValueError("console spool 不使用 connector tail recovery")
        if (not isinstance(audit_name, str) or not audit_name or "/" in audit_name
                or "\x00" in audit_name or audit_name in (".", "..")):
            raise ValueError("tail recovery audit 文件名非法")
        with self._claimed_state() as (_work_fd, state_fd):
            inbox_fd = _open_optional_private_regular(
                state_fd, self._inbox_name, label=f"{self._label} inbox")
            if inbox_fd is None:
                return None
            audit_fd = -1
            try:
                info = os.fstat(inbox_fd)
                size = info.st_size
                if size == 0 or os.pread(inbox_fd, 1, size - 1) == b"\n":
                    return None
                position = size
                boundary = 0
                while position > 0:
                    start = max(0, position - 1024 * 1024)
                    chunk = os.pread(inbox_fd, position - start, start)
                    if len(chunk) != position - start:
                        raise UnsafeConsolePath(
                            f"{self._label} inbox tail 恢复读取被截断")
                    index = chunk.rfind(b"\n")
                    if index >= 0:
                        boundary = start + index + 1
                        break
                    position = start
                digest = hashlib.sha256()
                position = boundary
                while position < size:
                    chunk = os.pread(inbox_fd, min(1024 * 1024, size - position), position)
                    if not chunk:
                        raise UnsafeConsolePath(
                            f"{self._label} inbox tail 恢复 hash 读取被截断")
                    digest.update(chunk)
                    position += len(chunk)
                record = {
                    "version": 1,
                    "kind": "uncommitted_tail_truncated",
                    "inbox_name": self._inbox_name,
                    "start_offset": boundary,
                    "byte_count": size - boundary,
                    "tail_hash": "sha256:" + digest.hexdigest(),
                }
                payload = (json.dumps(
                    record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    allow_nan=False) + "\n").encode("utf-8")
                audit_fd, _created = _open_exclusive_or_existing(
                    state_fd, audit_name, _FILE_RW_FLAGS, 0o600)
                audit_info = _verify_entry_matches_fd(
                    state_fd, audit_name, audit_fd,
                    label=f"{self._label} tail recovery audit", regular=True)
                if audit_info.st_uid != os.geteuid():
                    raise UnsafeConsolePath(
                        f"{self._label} tail recovery audit owner 不是当前 uid")
                os.fchmod(audit_fd, 0o600)
                audit_size = os.fstat(audit_fd).st_size
                if audit_size and os.pread(audit_fd, 1, audit_size - 1) != b"\n":
                    # The prior recovery attempt did not durably commit its
                    # audit row and also did not remove the inbox tail.  Drop
                    # only that uncommitted audit suffix, then regenerate the
                    # same hash-only evidence from the still-present inbox.
                    audit_position = audit_size
                    audit_boundary = 0
                    while audit_position > 0:
                        audit_start = max(0, audit_position - 1024 * 1024)
                        audit_chunk = os.pread(
                            audit_fd, audit_position - audit_start, audit_start)
                        audit_index = audit_chunk.rfind(b"\n")
                        if audit_index >= 0:
                            audit_boundary = audit_start + audit_index + 1
                            break
                        audit_position = audit_start
                    os.ftruncate(audit_fd, audit_boundary)
                    os.fsync(audit_fd)
                    audit_size = audit_boundary
                if audit_size + len(payload) > max_audit_bytes:
                    raise UnsafeConsolePath(
                        f"{self._label} tail recovery audit 超过大小上限")
                _write_all(audit_fd, payload)
                os.fsync(audit_fd)
                os.fsync(state_fd)
                os.ftruncate(inbox_fd, boundary)
                os.fsync(inbox_fd)
                os.fsync(state_fd)
                _verify_entry_matches_fd(
                    state_fd, self._inbox_name, inbox_fd,
                    label=f"{self._label} inbox", regular=True)
                return record
            finally:
                if audit_fd >= 0:
                    os.close(audit_fd)
                os.close(inbox_fd)


class ConsoleSpool(DurableInboxSpool):
    """The browser/console trust domain (backward-compatible filenames)."""

    def __init__(self, work_root: Union[str, Path]):
        super().__init__(
            work_root, lock_name=_LOCK_NAME, inbox_name=_INBOX_NAME,
            cursor_name=_CURSOR_NAME, retry_name=_RETRY_NAME,
            key_prefix="console", label="console", capability_name=CAPABILITY_NAME,
            fence_uncommitted_tail=True)


class ConnectorSpool(DurableInboxSpool):
    """One authenticated connector channel's isolated ingress trust domain."""

    def __init__(self, work_root: Union[str, Path], channel: str):
        if (not isinstance(channel, str) or not channel
                or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in channel)):
            raise ValueError("connector spool channel 非法")
        stem = f"connector_{channel}_inbox"
        self.channel = channel
        self.quarantine_name = f"connector_{channel}_quarantine.jsonl"
        self.recovery_name = f"connector_{channel}_recovery.jsonl"
        super().__init__(
            work_root, lock_name=f".connector-{channel}-inbox.lock",
            inbox_name=stem + ".jsonl", cursor_name=stem + ".cursor",
            retry_name=f".{stem}.retry.json", key_prefix="connector",
            label=f"connector {channel}")

    def record_quarantine(self, record: Dict[str, Any]) -> None:
        self.append_audit_record(self.quarantine_name, record)

    def quarantine_records(self) -> List[Dict[str, Any]]:
        return self.scan_audit_records(self.quarantine_name)

    def repair_uncommitted_inbox_tail(self) -> Optional[Dict[str, Any]]:
        return self.repair_uncommitted_tail(self.recovery_name)

    def recovery_records(self) -> List[Dict[str, Any]]:
        return self.scan_audit_records(self.recovery_name)


def normalize_upload_ref(source_ref: Any) -> Tuple[str, str, Tuple[str, ...]]:
    if not isinstance(source_ref, str):
        raise ValueError("resolve 须提供字符串 source_ref")
    ref = source_ref.strip().rstrip("/")
    if len(ref) > _MAX_SOURCE_REF_CHARS:
        raise ValueError(f"source_ref 过长（最多 {_MAX_SOURCE_REF_CHARS} 字符）")
    if (not ref or ref.startswith("/") or "\\" in ref
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in ref)):
        raise ValueError("source_ref 须为 work/uploads/... 或 input/uploads/...")
    parts = ref.split("/")
    if any(part in ("", ".", "..") for part in parts) or len(parts) < 2:
        raise ValueError("source_ref 路径非法（不得含空段、. 或 ..）")
    if parts[0] not in ("work", "input") or parts[1] != "uploads":
        raise ValueError("source_ref 只允许 work/uploads/... 或 input/uploads/...")
    return "/".join(parts), parts[0], tuple(parts[2:])


class PinnedUploadDirectory:
    """An open upload directory whose procfs path remains bound to its inode."""

    def __init__(self, *, normalized_ref: str, fd: int):
        self.normalized_ref = normalized_ref
        self._fd = fd

    @property
    def fd(self) -> int:
        if self._fd < 0:
            raise ValueError("upload capability 已关闭")
        return self._fd

    @property
    def proc_path(self) -> str:
        fd = self.fd
        path = f"/proc/self/fd/{fd}"
        if not os.path.isdir(path):
            raise UnsafeConsolePath("/proc/self/fd upload capability 不可用")
        return path

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> "PinnedUploadDirectory":
        self.proc_path                         # fail before handing capability to caller
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def open_pinned_upload_ref(source_ref: Any, *, work_root: Union[str, Path],
                           system_root: Union[str, Path]) -> PinnedUploadDirectory:
    """Open a virtual upload directory without following any path symlink."""
    normalized, virtual_root, tail = normalize_upload_ref(source_ref)
    base = Path(work_root) if virtual_root == "work" else Path(system_root) / "input"
    base_fd = open_directory_path(base, label=f"{virtual_root} upload root")
    try:
        final_fd = _open_directory_components(
            base_fd, ("uploads",) + tail, label="source_ref upload directory")
    except BaseException:
        os.close(base_fd)
        raise
    os.close(base_fd)
    return PinnedUploadDirectory(normalized_ref=normalized, fd=final_fd)


def _relative_components(relative: str) -> Tuple[str, ...]:
    if not isinstance(relative, str) or "\x00" in relative or "\\" in relative:
        raise UnsafeConsolePath("相对路径非法")
    if relative.startswith("/"):
        raise UnsafeConsolePath("相对路径不得为绝对路径")
    parts = tuple(relative.split("/"))
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise UnsafeConsolePath("相对路径不得含空段、. 或 ..")
    return parts


def read_regular_file_beneath(root: Union[str, Path], relative: str, *, max_bytes: int,
                              tail: bool = False) -> bytes:
    """Read one bounded, single-link regular file through its verified fd.

    ``tail=True`` retains only the newest complete-record window.  If reading
    begins in the middle of a physical LF-delimited record, that first fragment
    is discarded.  This is used for append-only observation logs, never for
    control intents.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes 须非负")
    parts = _relative_components(relative)
    root_fd = open_directory_path(root, label="read root")
    parent_fd = -1
    file_fd = -1
    try:
        parent_fd = _open_directory_components(root_fd, parts[:-1], label="read path directory")
        try:
            file_fd = os.open(parts[-1], _FILE_READ_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENXIO, errno.ENODEV):
                raise UnsafeConsolePath("读取目标不得是 symlink/FIFO/device") from error
            raise
        info = _verify_entry_matches_fd(parent_fd, parts[-1], file_fd,
                                        label="读取目标", regular=True)
        if info.st_size > max_bytes and not tail:
            raise ValueError(f"文件超过读取上限 {max_bytes} 字节")
        start = max(0, info.st_size - max_bytes) if tail else 0
        os.lseek(file_fd, start, os.SEEK_SET)
        data = _read_all(file_fd, max_bytes=max_bytes)
        if tail and start:
            boundary = data.find(b"\n")
            return b"" if boundary < 0 else data[boundary + 1:]
        return data
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(root_fd)


def stat_regular_file_beneath(root: Union[str, Path], relative: str) -> os.stat_result:
    """Return ``fstat`` for a regular file reached only through verified dirfds.

    The snapshot belongs to the descriptor opened with ``O_NOFOLLOW``; callers
    must consume it directly instead of reconstructing and re-statting a path.
    """
    parts = _relative_components(relative)
    root_fd = open_directory_path(root, label="stat root")
    parent_fd = -1
    file_fd = -1
    try:
        parent_fd = _open_directory_components(root_fd, parts[:-1], label="stat path directory")
        try:
            file_fd = os.open(parts[-1], _FILE_READ_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENXIO, errno.ENODEV):
                raise UnsafeConsolePath("stat 目标不得是 symlink/FIFO/device") from error
            raise
        return _verify_entry_matches_fd(
            parent_fd, parts[-1], file_fd, label="stat 目标", regular=True)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(root_fd)
