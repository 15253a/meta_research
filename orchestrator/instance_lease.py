"""Process-wide owner lease and observable heartbeat for one work root.

SQLite serializes transactions, but it cannot stop two orchestrators from
performing the same expensive model/execution call before either transaction
commits.  The stable ``flock`` in this module is therefore the authority for
all process-global side effects (DB writer, connector consumers/listeners,
outbox delivery and external calls).  PID/heartbeat metadata is diagnostic
only: takeover is decided exclusively by the kernel lock, never by guessing
that a PID or timestamp is stale.

Linux is an explicit runtime requirement (``flock``, ``/proc`` and openat with
``O_NOFOLLOW`` are already used by the control-plane filesystem code).
"""
from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import secrets
import signal
import socket
import stat
import threading
import time
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .console_spool import (
    UnsafeConsolePath,
    _atomic_write_private_sidecar,
    _ensure_private_state,
    _verify_entry_matches_fd,
    open_directory_path,
)


LOCK_NAME = ".orchestrator-instance.lock"
HEARTBEAT_NAME = "orchestrator_heartbeat.json"
HEARTBEAT_REF = "state/" + HEARTBEAT_NAME
_LOCK_MAX_BYTES = 16 * 1024
_HEARTBEAT_MAX_AGE_S = 5.0
_LOCK_FLAGS = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
               | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
               | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
_STATES = frozenset({"starting", "ready", "running", "paused", "stopping", "stopped"})
_ACTIVE_LEASES: "weakref.WeakSet[InstanceLease]" = weakref.WeakSet()
# A failed constructor cannot return its lease normally.  Keep an incomplete
# cleanup strongly reachable (and therefore fail-closed) until the exception's
# retry handle finishes it or the process exits.
_RETAINED_LEASES: set = set()
_ACTIVE_LEASES_GUARD = threading.Lock()
# A cleanup guardian must inherit one duplicate of the flock open-file
# description, while every unrelated fork child must close such duplicates.
# Serialize the short duplicate->spawn window with at-fork and identify the
# one descriptor deliberately delegated to that spawn.  This is an RLock
# because subprocess may call os.fork() in the same thread while the caller is
# already inside ``delegate_owner_fence``.
_DELEGATED_FENCE_GUARD = threading.RLock()
_DELEGATED_FENCE_FDS: set = set()
_INTENDED_FENCE_FD: Optional[int] = None


class InstanceLeaseError(RuntimeError):
    """The owner capability or its observable heartbeat is no longer safe."""


class InstanceBusyError(InstanceLeaseError):
    """Another process currently owns the stable work-root lease."""

    def __init__(self, message: str, *, owner: Optional[Dict[str, Any]] = None):
        self.owner = owner
        super().__init__(message)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _strict_json(raw: bytes) -> Dict[str, Any]:
    def unique_object(pairs):  # noqa: ANN001
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"重复 JSON key: {key}")
            value[key] = item
        return value

    if len(raw) > _LOCK_MAX_BYTES:
        raise ValueError("instance metadata 超过大小上限")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"非有限 JSON number: {token}")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("instance metadata 不是严格 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("instance metadata 须为 object")
    return value


def _read_fd_bounded(fd: int, *, max_bytes: int = _LOCK_MAX_BYTES) -> bytes:
    size = os.fstat(fd).st_size
    if size < 0 or size > max_bytes:
        raise ValueError("instance metadata 超过大小上限")
    chunks = []
    position = 0
    while position < size:
        chunk = os.pread(fd, min(4096, size - position), position)
        if not chunk:
            raise ValueError("instance metadata 读取被截断")
        chunks.append(chunk)
        position += len(chunk)
    return b"".join(chunks)


def _boot_id() -> Optional[str]:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii").strip().lower()
    except (OSError, UnicodeError):
        return None
    if (len(value) != 36
            or any(ch not in "0123456789abcdef-" for ch in value)):
        return None
    return value


def _process_start_ticks(pid: Optional[int] = None) -> str:
    target = "self" if pid is None else str(pid)
    try:
        raw = Path(f"/proc/{target}/stat").read_text(encoding="ascii")
        # comm is parenthesized and may contain spaces or ')'; fields after
        # the final ')' start at field 3, so starttime(field 22) is index 19.
        closing = raw.rfind(")")
        if closing < 1:
            raise ValueError("proc stat comm 边界缺失")
        rest = raw[closing + 2:].split()
        value = rest[19]
    except (OSError, UnicodeError, IndexError, ValueError) as error:
        raise InstanceLeaseError("无法读取当前进程 start ticks") from error
    if not value.isascii() or not value.isdigit():
        raise InstanceLeaseError("当前进程 start ticks 非法")
    return value


def _safe_hostname() -> str:
    value = socket.gethostname()
    cleaned = "".join(ch for ch in value if 0x21 <= ord(ch) <= 0x7e)
    return (cleaned or "unknown")[:255]


def _canonical_bytes(value: Dict[str, Any]) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False) + "\n").encode("utf-8")


class InstanceLease:
    """Held owner capability; close it only after every shared resource stops."""

    def __init__(self, *, work_root: Path, work_fd: int, state_fd: int,
                 lock_fd: int, lock_info: os.stat_result,
                 owner: Dict[str, Any], heartbeat_interval_s: float):
        self.work_root = work_root
        self._work_fd = work_fd
        self._state_fd = state_fd
        self._lock_fd = lock_fd
        self._lock_info = lock_info
        self.owner = owner
        self.owner_id = owner["owner_id"]
        self._process_start = owner["process_start_ticks"]
        self._interval = heartbeat_interval_s
        self._state_guard = threading.RLock()
        self._write_guard = threading.Lock()
        self._close_guard = threading.Lock()
        self._stop = threading.Event()
        self._heartbeat_fatal: Optional[BaseException] = None
        self._closing = False
        self._desired_state = "starting"
        self._cycle_id: Optional[str] = None
        self._stage: Optional[str] = None
        self._activity: Optional[str] = "assembly"
        self._sequence = 0
        self._closed = False
        self._thread = threading.Thread(
            # System.close() remains the orderly shutdown path.  As a final
            # process-exit property, however, this observer must not keep a
            # crashed CLI alive solely to retain a flock the kernel can safely
            # release when the process exits.
            target=self._heartbeat_loop, daemon=True,
            name="orchestrator-owner-heartbeat")

    @classmethod
    def acquire(cls, work_root: Union[str, Path], *,
                heartbeat_interval_s: float = 1.0) -> "InstanceLease":
        if (isinstance(heartbeat_interval_s, bool)
                or not isinstance(heartbeat_interval_s, (int, float))
                or not 0.02 <= float(heartbeat_interval_s) <= 60.0):
            raise ValueError("heartbeat_interval_s 须在 [0.02,60] 内")
        # Lexical absolute path preserves the acquired pathname identity across
        # later chdir() without resolving/following a symlink component.
        root = Path(os.path.abspath(os.fspath(work_root)))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        work_fd = open_directory_path(root, label="orchestrator work_root")
        state_fd = -1
        lock_fd = -1
        locked = False
        lease: Optional[InstanceLease] = None
        # One structured critical section covers every transition between
        # "authority FD exists" and "fork registry reflects that FD".  The
        # finally removes async-KeyboardInterrupt gaps around manual release.
        _ACTIVE_LEASES_GUARD.acquire()
        try:
            work_info = os.fstat(work_fd)
            if work_info.st_uid != os.geteuid():
                raise InstanceLeaseError("work_root owner 不是当前 uid")
            os.fchmod(work_fd, 0o700)
            current_root = os.stat(root, follow_symlinks=False)
            if not stat.S_ISDIR(current_root.st_mode) or not _same_inode(work_info, current_root):
                raise InstanceLeaseError("work_root 路径身份发生变化")

            # From the first authority FD open through registry publication,
            # block fork's before-handler.  Otherwise a concurrent child can
            # inherit an unregistered flock OFD and retain it after parent exit.
            try:
                lock_fd = os.open(
                    LOCK_NAME, _LOCK_FLAGS | os.O_EXCL, 0o600, dir_fd=work_fd)
                created_lock = True
            except FileExistsError:
                lock_fd = os.open(LOCK_NAME, _LOCK_FLAGS, 0o600, dir_fd=work_fd)
                created_lock = False
            if created_lock:
                os.fchmod(lock_fd, 0o600)
            lock_info = _verify_entry_matches_fd(
                work_fd, LOCK_NAME, lock_fd,
                label="orchestrator instance lock", regular=True)
            if lock_info.st_uid != os.geteuid():
                raise InstanceLeaseError("orchestrator instance lock owner 不是当前 uid")
            if stat.S_IMODE(lock_info.st_mode) != 0o600:
                raise InstanceLeaseError("orchestrator instance lock mode 必须为 0600")
            lock_error: Optional[OSError] = None
            # A read-only status probe briefly takes this same flock to test
            # whether an owner exists.  Retry a few milliseconds so that
            # observability cannot create a false "second orchestrator" busy
            # result; a real owner remains locked throughout every attempt.
            for attempt in range(4):
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    lock_error = error
                    if attempt < 3:
                        time.sleep(0.005)
            if not locked:
                owner = None
                try:
                    owner = _strict_json(_read_fd_bounded(lock_fd))
                except ValueError:
                    pass
                detail = ""
                if isinstance(owner, dict):
                    detail = (f" owner_id={str(owner.get('owner_id', 'unknown'))[:64]}"
                              f" pid={owner.get('pid', 'unknown')}")
                raise InstanceBusyError(
                    "同一 work_root 已由另一 orchestrator 持有；拒绝双实例" + detail,
                    owner=owner) from lock_error
            # Re-check after flock wait/claim; a same-uid attacker must not
            # swap the pathname and leave us locking an unlinked old inode.
            lock_info = _verify_entry_matches_fd(
                work_fd, LOCK_NAME, lock_fd,
                label="orchestrator instance lock", regular=True)
            # Invalidate the previous generation immediately after claiming
            # flock.  Until the new owner metadata and heartbeat are both
            # published, observers must degrade to invalid—not report a fresh
            # heartbeat belonging to the dead previous holder.
            os.ftruncate(lock_fd, 0)
            os.fsync(lock_fd)
            state_fd, _created_state = _ensure_private_state(work_fd)
            owner = {
                "version": 1,
                "owner_id": "owner-" + secrets.token_hex(16),
                "hostname": _safe_hostname(),
                "boot_id": _boot_id(),
                "pid": os.getpid(),
                "process_start_ticks": _process_start_ticks(),
                "acquired_at_unix": time.time(),
                "work_root_dev": work_info.st_dev,
                "work_root_ino": work_info.st_ino,
                "heartbeat_interval_s": float(heartbeat_interval_s),
                "heartbeat_deadline_s": max(
                    _HEARTBEAT_MAX_AGE_S, float(heartbeat_interval_s) * 3.0),
            }
            payload = _canonical_bytes(owner)
            os.ftruncate(lock_fd, 0)
            os.lseek(lock_fd, 0, os.SEEK_SET)
            view = memoryview(payload)
            while view:
                written = os.write(lock_fd, view)
                if written <= 0:
                    raise OSError("instance lock metadata 写入停滞")
                view = view[written:]
            os.fsync(lock_fd)
            os.fsync(work_fd)
            lease = cls(
                work_root=root, work_fd=work_fd, state_fd=state_fd,
                lock_fd=lock_fd, lock_info=lock_info, owner=owner,
                heartbeat_interval_s=float(heartbeat_interval_s))
            lease._write_heartbeat()
            _ACTIVE_LEASES.add(lease)
            try:
                lease._thread.start()
            except BaseException:
                lease._stop.set()
                raise
            return lease
        except BaseException as primary:
            cleanup_errors = []
            cleanup_incomplete = False
            if lease is not None:
                # ``Thread.start`` may have succeeded before an asynchronous
                # exception reached this frame.  A started heartbeat writer is
                # a shared capability: stop/join it before releasing flock.
                with lease._close_guard:
                    if lease._thread.ident is not None:
                        stop_error = lease._stop_heartbeat_for_close_locked()
                    else:
                        with lease._state_guard:
                            lease._closing = True
                        lease._stop.set()
                        stop_error = None
                if stop_error is not None:
                    cleanup_errors.append(stop_error)
                else:
                    try:
                        # acquire() already owns the fork registry guard.
                        lease._release_descriptors_locked()
                    except BaseException as error:
                        cleanup_errors.append(error)
                if lease.closed:
                    state_fd = work_fd = lock_fd = -1
                else:
                    cleanup_incomplete = True
                    _ACTIVE_LEASES.add(lease)
                    _RETAINED_LEASES.add(lease)
                    try:
                        primary.orchestrator_cleanup = lease
                    except BaseException as error:
                        cleanup_errors.append(error)
            else:
                # No heartbeat thread/object exists yet.  Best-effort close
                # continues through BaseException and keeps lock FD last.
                for fd in (state_fd, work_fd, lock_fd):
                    if fd < 0:
                        continue
                    try:
                        os.close(fd)
                    except BaseException as error:
                        cleanup_errors.append(error)
            add_note = getattr(primary, "add_note", None)
            if callable(add_note):
                for error in cleanup_errors:
                    add_note(
                        "instance acquire rollback close 失败: "
                        f"{type(error).__name__}: {error}")
                if cleanup_incomplete:
                    add_note(
                        "instance acquire rollback 未完成；flock 已保留，"
                        "可调用 error.orchestrator_cleanup.close() 重试")
            # Keep one public, fail-closed exception contract for unsafe entry
            # types and low-level path/open failures.  Busy/ownership failures
            # already inherit from InstanceLeaseError and pass through below.
            if isinstance(primary, (UnsafeConsolePath, OSError)):
                wrapped = InstanceLeaseError(
                    "orchestrator instance lease 路径不安全或无法打开")
                if cleanup_incomplete and lease is not None:
                    # The wrapper is the exception callers actually catch; do
                    # not hide the only retry capability on ``__cause__``.
                    wrapped.orchestrator_cleanup = lease
                wrapped_note = getattr(wrapped, "add_note", None)
                if callable(wrapped_note):
                    for error in cleanup_errors:
                        wrapped_note(
                            "instance acquire rollback close 失败: "
                            f"{type(error).__name__}: {error}")
                    if cleanup_incomplete:
                        wrapped_note(
                            "instance acquire rollback 未完成；flock 已保留，"
                            "可调用 error.orchestrator_cleanup.close() 重试")
                raise wrapped from primary
            raise
        finally:
            _ACTIVE_LEASES_GUARD.release()

    def _heartbeat_payload(self) -> Dict[str, Any]:
        with self._state_guard:
            return {
                "version": 1,
                "owner_id": self.owner_id,
                "hostname": self.owner["hostname"],
                "boot_id": self.owner["boot_id"],
                "pid": self.owner["pid"],
                "process_start_ticks": self._process_start,
                "heartbeat_interval_s": self.owner["heartbeat_interval_s"],
                "heartbeat_deadline_s": self.owner["heartbeat_deadline_s"],
                "state": self._desired_state,
                "sequence": self._sequence + 1,
                "updated_at_unix": time.time(),
                "updated_monotonic_s": time.monotonic(),
                "cycle_id": self._cycle_id,
                "stage": self._stage,
                "activity": self._activity,
            }

    def _write_heartbeat(self) -> None:
        with self._write_guard:
            payload = self._heartbeat_payload()
            _atomic_write_private_sidecar(
                self._state_fd, HEARTBEAT_NAME, _canonical_bytes(payload),
                label="orchestrator heartbeat")
            with self._state_guard:
                self._sequence = int(payload["sequence"])

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.assert_owned(check_heartbeat=False)
                self._write_heartbeat()
            except BaseException as error:
                with self._state_guard:
                    if self._heartbeat_fatal is None:
                        self._heartbeat_fatal = error
                self._stop.set()
                return

    def set_state(self, state: str, *, cycle_id: Optional[str] = None,
                  stage: Optional[str] = None, activity: Optional[str] = None) -> None:
        if state not in _STATES:
            raise ValueError(f"instance heartbeat state 非法: {state!r}")
        for label, value, limit in (
                ("cycle_id", cycle_id, 64), ("stage", stage, 64),
                ("activity", activity, 256)):
            if (value is not None
                    and (not isinstance(value, str) or not value or len(value) > limit
                         or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value))):
                raise ValueError(f"instance heartbeat {label} 非法")
        with self._close_guard:
            with self._state_guard:
                if self._closing or self._closed:
                    raise InstanceLeaseError("orchestrator instance lease 正在/已经关闭")
            self.assert_owned()
            with self._state_guard:
                self._desired_state = state
                self._cycle_id = cycle_id
                self._stage = stage
                self._activity = activity
            self._write_heartbeat()

    def assert_owned(self, *, check_heartbeat: bool = True) -> None:
        with self._state_guard:
            if self._closed or self._lock_fd < 0:
                raise InstanceLeaseError("orchestrator instance lease 已释放")
            if self._closing and check_heartbeat:
                raise InstanceLeaseError("orchestrator instance lease 正在关闭")
            heartbeat_fatal = self._heartbeat_fatal
        if check_heartbeat and heartbeat_fatal is not None:
            raise InstanceLeaseError(
                f"orchestrator heartbeat 已失败: {type(heartbeat_fatal).__name__}") \
                from heartbeat_fatal
        if os.getpid() != self.owner["pid"] or _process_start_ticks() != self._process_start:
            raise InstanceLeaseError("instance lease 不得跨 fork/PID identity 使用")
        try:
            lock_info = os.fstat(self._lock_fd)
            lock_path = os.stat(LOCK_NAME, dir_fd=self._work_fd, follow_symlinks=False)
            root_path = os.stat(self.work_root, follow_symlinks=False)
            root_info = os.fstat(self._work_fd)
            state_path = os.stat("state", dir_fd=self._work_fd, follow_symlinks=False)
            state_info = os.fstat(self._state_fd)
        except OSError as error:
            raise InstanceLeaseError("instance lease 路径身份无法复核") from error
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or not _same_inode(lock_info, self._lock_info)
                or not _same_inode(lock_info, lock_path)
                or lock_info.st_uid != os.geteuid()
                or stat.S_IMODE(lock_info.st_mode) != 0o600):
            raise InstanceLeaseError("instance lock inode 已被替换/解绑")
        if (not stat.S_ISDIR(root_path.st_mode)
                or not _same_inode(root_path, root_info)
                or (root_path.st_dev, root_path.st_ino)
                != (self.owner["work_root_dev"], self.owner["work_root_ino"])
                or root_info.st_uid != os.geteuid()
                or stat.S_IMODE(root_info.st_mode) != 0o700):
            raise InstanceLeaseError("work_root inode 已被替换")
        if (not stat.S_ISDIR(state_path.st_mode) or not _same_inode(state_path, state_info)
                or state_info.st_uid != os.geteuid()
                or stat.S_IMODE(state_info.st_mode) != 0o700):
            raise InstanceLeaseError("instance state 目录已被替换")

    @contextmanager
    def delegate_owner_fence(self):
        """Yield one spawn-only duplicate of the owner flock to a guardian.

        The duplicate keeps the same Linux flock open-file description alive
        after an orchestrator SIGKILL.  It is intentionally available only as
        a context around the guardian spawn: unrelated fork children are
        closed by the module at-fork hook, the workload is never given this
        descriptor, and the parent copy is closed immediately after spawn.
        ``close()`` is serialized until that hand-off has completed.
        """
        global _INTENDED_FENCE_FD
        # Ctrl-C is the realistic async exception source in the parent.  Keep
        # it pending only across this short duplicate->Popen->parent-close
        # critical section; the guardian explicitly unblocks signals before it
        # starts the payload.  Pending SIGINT is delivered after every lock and
        # parent duplicate has been cleaned.
        old_mask = None
        if hasattr(signal, "pthread_sigmask"):
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        delegated_acquired = close_acquired = False
        fence_fd = -1
        primary: Optional[BaseException] = None
        cleanup_errors = []
        try:
            _DELEGATED_FENCE_GUARD.acquire()
            delegated_acquired = True
            self._close_guard.acquire()
            close_acquired = True
            if _INTENDED_FENCE_FD is not None:
                raise InstanceLeaseError("instance owner fence 不允许嵌套委托")
            self.assert_owned()
            fence_fd = os.dup(self._lock_fd)
            os.set_inheritable(fence_fd, False)
            if not _same_inode(os.fstat(fence_fd), self._lock_info):
                raise InstanceLeaseError("instance owner fence duplicate 身份不一致")
            _DELEGATED_FENCE_FDS.add(fence_fd)
            _INTENDED_FENCE_FD = fence_fd
            yield fence_fd
        except BaseException as error:
            # Setup failures (guard/acquire/assert/dup) are just as primary as
            # an exception injected through the context body.  Cleanup or a
            # pending signal delivered while restoring the mask must not erase
            # the first cause.
            primary = error
            raise
        finally:
            # Keep converging through injected/repeated BaseException.  These
            # assignments/discards are idempotent; close itself is attempted
            # once because Linux close(EINTR) must not be retried.
            while True:
                try:
                    _INTENDED_FENCE_FD = None
                    break
                except BaseException as error:
                    cleanup_errors.append(error)
            if fence_fd >= 0:
                while True:
                    try:
                        _DELEGATED_FENCE_FDS.discard(fence_fd)
                        break
                    except BaseException as error:
                        cleanup_errors.append(error)
                try:
                    os.close(fence_fd)
                except BaseException as error:
                    cleanup_errors.append(error)
            if close_acquired:
                while True:
                    try:
                        self._close_guard.release()
                        break
                    except BaseException as error:
                        cleanup_errors.append(error)
            if delegated_acquired:
                while True:
                    try:
                        _DELEGATED_FENCE_GUARD.release()
                        break
                    except BaseException as error:
                        cleanup_errors.append(error)
            restore_error: Optional[BaseException] = None
            if old_mask is not None:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
                except BaseException as error:
                    # A pending SIGINT may be raised by the restoration call
                    # after the kernel has already restored the mask.  Preserve
                    # that original signal exception once every FD/lock is safe.
                    restore_error = error
            if primary is not None:
                add_note = getattr(primary, "add_note", None)
                if callable(add_note):
                    for error in [*cleanup_errors, *([restore_error] if restore_error else [])]:
                        add_note(
                            "owner fence cleanup 期间异常: "
                            f"{type(error).__name__}: {error}")
            elif restore_error is not None:
                add_note = getattr(restore_error, "add_note", None)
                if callable(add_note):
                    for error in cleanup_errors:
                        add_note(
                            "owner fence cleanup 期间异常: "
                            f"{type(error).__name__}: {error}")
                raise restore_error
            elif cleanup_errors:
                error = InstanceLeaseError("instance owner fence cleanup 失败")
                add_note = getattr(error, "add_note", None)
                if callable(add_note):
                    for item in cleanup_errors:
                        add_note(f"{type(item).__name__}: {item}")
                raise error

    @property
    def heartbeat_fatal(self) -> Optional[BaseException]:
        with self._state_guard:
            return self._heartbeat_fatal

    @property
    def closed(self) -> bool:
        with self._state_guard:
            return self._closed

    @property
    def closing(self) -> bool:
        with self._state_guard:
            return self._closing

    def _stop_heartbeat_for_close_locked(self) -> Optional[BaseException]:
        """Publish stopped and join; caller serializes with ``_close_guard``."""
        with self._state_guard:
            self._closing = True
            self._desired_state = "stopped"
            self._cycle_id = None
            self._stage = None
            self._activity = None
        try:
            self._write_heartbeat()
        except BaseException as error:
            heartbeat_error = InstanceLeaseError(
                "orchestrator stopped heartbeat 发布失败；instance lease 保留供重试")
            heartbeat_error.__cause__ = error
        else:
            heartbeat_error = None
        self._stop.set()
        try:
            self._thread.join(timeout=max(1.0, self._interval + 1.0))
        except BaseException as error:
            join_error = InstanceLeaseError(
                "orchestrator heartbeat thread join 失败；instance lease 保留供重试")
            join_error.__cause__ = error
            return join_error
        if self._thread.is_alive():
            return InstanceLeaseError("orchestrator heartbeat thread 未在 deadline 内停止")
        return heartbeat_error

    def _release_descriptors_locked(self) -> None:
        """Close inherited-capable FDs while caller holds the fork guard.

        POSIX signals become ``KeyboardInterrupt`` in the main thread.  Do the
        irreversible Linux close sequence in a short non-daemon worker, then
        keep joining through main-thread interruption.  Thus an interruption
        can be reported only after the lock FD is closed last and the object is
        tombstoned; it can never strand an untracked flock OFD.
        """
        # Caller has already stopped/joined the heartbeat.  The lock FD is
        # deliberately last: no new owner may start while an old heartbeat can
        # still write or while its state/work capabilities remain in use.
        descriptors = [self._state_fd, self._work_fd, self._lock_fd]
        errors = []
        release_done = threading.Event()

        def release() -> None:
            try:
                # Descriptor release is irrevocable on Linux (close(EINTR)
                # must not be retried because the numeric FD may already be
                # reused).
                for fd in descriptors:
                    if fd < 0:
                        continue
                    try:
                        os.close(fd)
                    except BaseException as error:
                        errors.append(error)
            finally:
                try:
                    self._state_fd = self._work_fd = self._lock_fd = -1
                    with self._state_guard:
                        self._closed = True
                except BaseException as error:
                    errors.append(error)
                finally:
                    release_done.set()

        releaser = threading.Thread(
            target=release, daemon=False, name="orchestrator-owner-release")
        try:
            releaser.start()
        except BaseException as error:
            # start() itself has an ambiguous interruption window.  A non-None
            # ident proves the worker owns finalization; otherwise retain every
            # descriptor and let close() retry.
            if releaser.ident is None:
                start_error = InstanceLeaseError(
                    "instance descriptor release worker 未启动；lease 保留供重试")
                start_error.__cause__ = error
                raise start_error
            errors.append(error)
        # Do not use an interruptible Thread.join as the completion authority:
        # on some CPython versions KeyboardInterrupt during join can make
        # thread liveness bookkeeping ambiguous.  The worker publishes this
        # event only after lock-last close and object tombstoning.
        while not release_done.is_set():
            try:
                release_done.wait(0.05)
            except BaseException as error:
                errors.append(error)
                continue
        while True:
            try:
                releaser.join()
                break
            except BaseException as error:
                errors.append(error)
                if not releaser.is_alive():
                    break
        while True:
            try:
                _ACTIVE_LEASES.discard(self)
                _RETAINED_LEASES.discard(self)
                break
            except BaseException as error:
                errors.append(error)
                continue
        if errors:
            primary = InstanceLeaseError("instance lease descriptor close 失败")
            add_note = getattr(primary, "add_note", None)
            if callable(add_note):
                for error in errors:
                    add_note(f"{type(error).__name__}: {error}")
            raise primary

    def _release_descriptors(self) -> None:
        with _ACTIVE_LEASES_GUARD:
            self._release_descriptors_locked()

    def _detach_after_fork_child(self) -> None:
        """Drop inherited OFD references without unlocking/writing as child."""
        descriptors = [self._state_fd, self._work_fd, self._lock_fd]
        self._state_fd = self._work_fd = self._lock_fd = -1
        for fd in descriptors:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        # Locks may have been held by threads that vanished at fork.  Replace
        # them so child-side close/assert fails immediately rather than hangs.
        self._state_guard = threading.RLock()
        self._write_guard = threading.Lock()
        self._close_guard = threading.Lock()
        self._stop = threading.Event()
        self._stop.set()
        self._closing = False
        self._closed = True

    def close(self) -> Optional[BaseException]:
        """Stop heartbeat and release the owner capability; idempotent.

        Publishing the terminal heartbeat and joining its writer are both
        part of the release fence.  Either failure retains every descriptor
        and the flock so cleanup can be retried without allowing a new owner
        to overlap an old writer.
        """
        with self._close_guard:
            if self.closed:
                return None
            heartbeat_error = self._stop_heartbeat_for_close_locked()
            if heartbeat_error is not None:
                return heartbeat_error
            try:
                self._release_descriptors()
            except BaseException as error:
                return error
            return None

    def __enter__(self) -> "InstanceLease":
        self.assert_owned()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:  # noqa: ANN001
        error = self.close()
        if error is not None and _exc is None:
            raise error


def _before_fork() -> None:
    _DELEGATED_FENCE_GUARD.acquire()
    _ACTIVE_LEASES_GUARD.acquire()


def _after_fork_parent() -> None:
    _ACTIVE_LEASES_GUARD.release()
    _DELEGATED_FENCE_GUARD.release()


def _after_fork_child() -> None:
    try:
        for lease in list(_ACTIVE_LEASES):
            lease._detach_after_fork_child()
        _ACTIVE_LEASES.clear()
        _RETAINED_LEASES.clear()
        # Only the child being exec'd as the cleanup guardian may retain the
        # deliberately delegated fence.  Every ordinary fork child drops all
        # duplicates so it cannot invisibly prolong the global owner lock.
        for fd in list(_DELEGATED_FENCE_FDS):
            if fd == _INTENDED_FENCE_FD:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
        _DELEGATED_FENCE_FDS.clear()
    finally:
        _ACTIVE_LEASES_GUARD.release()
        _DELEGATED_FENCE_GUARD.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child)


def _finite_number(value: Any, *, minimum: Optional[float] = None,
                   maximum: Optional[float] = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return (math.isfinite(number)
            and (minimum is None or number >= minimum)
            and (maximum is None or number <= maximum))


def _valid_owner_metadata(owner: Dict[str, Any]) -> bool:
    required = {
        "version", "owner_id", "hostname", "boot_id", "pid",
        "process_start_ticks", "acquired_at_unix", "work_root_dev",
        "work_root_ino", "heartbeat_interval_s", "heartbeat_deadline_s",
    }
    owner_id = owner.get("owner_id")
    boot_id = owner.get("boot_id")
    interval = owner.get("heartbeat_interval_s")
    deadline = owner.get("heartbeat_deadline_s")
    return (
        required <= set(owner)
        and isinstance(owner.get("version"), int)
        and not isinstance(owner["version"], bool) and owner["version"] == 1
        and isinstance(owner_id, str) and len(owner_id) == 38
        and owner_id.startswith("owner-")
        and all(ch in "0123456789abcdef" for ch in owner_id[6:])
        and isinstance(owner.get("hostname"), str) and bool(owner["hostname"])
        and (boot_id is None or (
            isinstance(boot_id, str) and len(boot_id) == 36
            and all(ch in "0123456789abcdef-" for ch in boot_id)))
        and isinstance(owner.get("pid"), int) and not isinstance(owner["pid"], bool)
        and owner["pid"] > 0
        and isinstance(owner.get("process_start_ticks"), str)
        and owner["process_start_ticks"].isascii()
        and owner["process_start_ticks"].isdigit()
        and _finite_number(owner.get("acquired_at_unix"), minimum=1.0)
        and isinstance(owner.get("work_root_dev"), int)
        and not isinstance(owner["work_root_dev"], bool) and owner["work_root_dev"] >= 0
        and isinstance(owner.get("work_root_ino"), int)
        and not isinstance(owner["work_root_ino"], bool) and owner["work_root_ino"] > 0
        and _finite_number(interval, minimum=0.02, maximum=60.0)
        and _finite_number(deadline, minimum=_HEARTBEAT_MAX_AGE_S, maximum=300.0)
        and float(deadline) == max(_HEARTBEAT_MAX_AGE_S, float(interval) * 3.0))


def _valid_heartbeat(heartbeat: Dict[str, Any]) -> bool:
    required = {
        "version", "owner_id", "hostname", "boot_id", "pid",
        "process_start_ticks", "heartbeat_interval_s", "heartbeat_deadline_s",
        "state", "sequence", "updated_at_unix", "updated_monotonic_s",
        "cycle_id", "stage", "activity",
    }
    boot_id = heartbeat.get("boot_id")
    hostname = heartbeat.get("hostname")

    def optional_text(value: Any, limit: int) -> bool:
        return (value is None or (
            isinstance(value, str) and bool(value) and len(value) <= limit
            and not any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)))

    return (
        required <= set(heartbeat)
        and isinstance(heartbeat.get("version"), int)
        and not isinstance(heartbeat["version"], bool) and heartbeat["version"] == 1
        and isinstance(heartbeat.get("owner_id"), str)
        and len(heartbeat["owner_id"]) == 38
        and heartbeat["owner_id"].startswith("owner-")
        and all(ch in "0123456789abcdef" for ch in heartbeat["owner_id"][6:])
        and isinstance(hostname, str) and bool(hostname) and len(hostname) <= 255
        and (boot_id is None or (
            isinstance(boot_id, str) and len(boot_id) == 36
            and all(ch in "0123456789abcdef-" for ch in boot_id)))
        and isinstance(heartbeat.get("pid"), int)
        and not isinstance(heartbeat["pid"], bool) and heartbeat["pid"] > 0
        and isinstance(heartbeat.get("process_start_ticks"), str)
        and heartbeat["process_start_ticks"].isascii()
        and heartbeat["process_start_ticks"].isdigit()
        and heartbeat.get("state") in _STATES
        and isinstance(heartbeat.get("sequence"), int)
        and not isinstance(heartbeat["sequence"], bool) and heartbeat["sequence"] >= 1
        and _finite_number(heartbeat.get("updated_at_unix"), minimum=1.0)
        and _finite_number(heartbeat.get("updated_monotonic_s"), minimum=0.0)
        and _finite_number(
            heartbeat.get("heartbeat_interval_s"), minimum=0.02, maximum=60.0)
        and _finite_number(
            heartbeat.get("heartbeat_deadline_s"),
            minimum=_HEARTBEAT_MAX_AGE_S, maximum=300.0)
        and optional_text(heartbeat.get("cycle_id"), 64)
        and optional_text(heartbeat.get("stage"), 64)
        and optional_text(heartbeat.get("activity"), 256))


def _read_regular_at(parent_fd: int, name: str, *, label: str,
                     max_bytes: int = _LOCK_MAX_BYTES) -> tuple[int, bytes, os.stat_result]:
    fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
    try:
        info = _verify_entry_matches_fd(
            parent_fd, name, fd, label=label, regular=True)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise UnsafeConsolePath(f"{label} owner/mode 非法")
        raw = _read_fd_bounded(fd, max_bytes=max_bytes)
        return fd, raw, info
    except BaseException:
        os.close(fd)
        raise


def read_instance_status(work_root: Union[str, Path], *,
                         max_age_s: Optional[float] = None) -> Dict[str, Any]:
    """Read a bounded diagnostic snapshot without treating metadata as authority."""
    if max_age_s is not None and not _finite_number(
            max_age_s, minimum=0.1, maximum=300.0):
        raise ValueError("instance max_age_s 非法")
    result: Dict[str, Any] = {
        "status": "inactive", "active": False, "lock_held": False,
        "owner_id": None, "pid": None, "state": None,
        "heartbeat_age_s": None, "sequence": None,
    }
    work_fd = lock_fd = state_fd = heartbeat_fd = -1
    try:
        work_fd = open_directory_path(work_root, label="orchestrator status work_root")
        lock_fd, _initial_raw, _lock_info = _read_regular_at(
            work_fd, LOCK_NAME, label="orchestrator instance lock")
        # This descriptor is opened only for observation.  Never pass an
        # owner's authority FD/OFD here: successful probe unlocks its own
        # open-file-description before returning inactive.  Shared probe locks
        # let concurrent observers coexist; a real owner/claimant holds/wants
        # EX, so it still excludes SH and acquire() briefly retries observer SH.
        for attempt in range(4):
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if attempt < 3:
                    time.sleep(0.005)
                    continue
                result["lock_held"] = True
            else:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                return result
            break

        # Re-read after the probe: an owner may have taken the stable inode and
        # replaced metadata between open/read and flock.  A final equality read
        # rejects another generation change while heartbeat is being read.
        lock_raw = _read_fd_bounded(lock_fd)
        owner = _strict_json(lock_raw)
        if not _valid_owner_metadata(owner):
            raise ValueError("instance owner metadata schema 非法")
        result["owner_id"] = owner["owner_id"]
        result["pid"] = owner["pid"]

        state_fd = os.open(
            "state", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=work_fd)
        state_info = _verify_entry_matches_fd(
            work_fd, "state", state_fd,
            label="orchestrator heartbeat state", regular=False)
        if (not stat.S_ISDIR(state_info.st_mode)
                or state_info.st_uid != os.geteuid()
                or stat.S_IMODE(state_info.st_mode) != 0o700):
            raise UnsafeConsolePath("orchestrator heartbeat state 目录 owner/mode 非法")
        heartbeat_fd, heartbeat_raw, _hb_info = _read_regular_at(
            state_fd, HEARTBEAT_NAME, label="orchestrator heartbeat")
        heartbeat = _strict_json(heartbeat_raw)
        if not _valid_heartbeat(heartbeat):
            raise ValueError("instance heartbeat schema 非法")

        result["state"] = heartbeat["state"]
        result["sequence"] = heartbeat["sequence"]
        current_boot = _boot_id()
        if current_boot is not None and heartbeat["boot_id"] == current_boot:
            age = time.monotonic() - float(heartbeat["updated_monotonic_s"])
        else:
            age = time.time() - float(heartbeat["updated_at_unix"])
        age_raw = age if -1.0 <= age < 10 ** 9 else None
        if age_raw is not None:
            result["heartbeat_age_s"] = round(max(0.0, age_raw), 3)
        matching = (
            heartbeat["owner_id"] == owner["owner_id"]
            and heartbeat["hostname"] == owner["hostname"]
            and heartbeat["boot_id"] == owner["boot_id"]
            and heartbeat["pid"] == owner["pid"]
            and heartbeat["process_start_ticks"] == owner["process_start_ticks"]
            and heartbeat["heartbeat_interval_s"] == owner["heartbeat_interval_s"]
            and heartbeat["heartbeat_deadline_s"] == owner["heartbeat_deadline_s"])
        deadline = (float(max_age_s) if max_age_s is not None
                    else float(owner["heartbeat_deadline_s"]))
        fresh = age_raw is not None and max(0.0, age_raw) <= deadline
        generation_stable = _read_fd_bounded(lock_fd) == lock_raw
        result["active"] = bool(
            generation_stable and matching and fresh and heartbeat["state"] != "stopped")
        if not generation_stable or not matching:
            result["status"] = "invalid"
        elif heartbeat["state"] == "stopped":
            result["status"] = "stopped_held"
        elif not fresh:
            result["status"] = "stale"
        else:
            result["status"] = "active"
        return result
    except FileNotFoundError:
        if result["lock_held"]:
            result["status"] = "invalid"
        return result
    except (ValueError, UnsafeConsolePath, OSError):
        result["active"] = False
        result["status"] = "invalid"
        return result
    finally:
        for fd in (heartbeat_fd, state_fd, lock_fd, work_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
