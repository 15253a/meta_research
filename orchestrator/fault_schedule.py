"""Foreground sidecar for one immutable, linear fault-injection schedule.

This module never launches or restarts ``orchestrator.run``.  The operator or
service manager owns that lifecycle.  The sidecar only observes an exact,
lease-fenced running execution, pins one local task with pidfd, publishes a
durable ``spent`` receipt, then sends SIGKILL once.

A durable ``spent`` without ``applied`` is inconclusive and never re-sends;
``applied`` can only resume aftermath observation.  Receipts explicitly keep
``signal_exactly_once=false``.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import math
import os
import re
import select
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .instance_lease import LOCK_NAME, _valid_owner_metadata
from .process_supervisor import validate_execution_receipt
from .qualification_firewall import (
    QualificationFirewallError,
    _canonical,
    _hash_bytes,
    _publish_once,
    _read_regular,
    _strict_json,
)


SCHEDULE_PROTOCOL = "meta-research-fault-schedule/v1"
SPENT_PROTOCOL = "meta-research-fault-spent/v1"
APPLIED_PROTOCOL = "meta-research-fault-applied/v1"
RESULT_PROTOCOL = "meta-research-fault-result/v1"
FINAL_PROTOCOL = "meta-research-fault-final/v1"
_SCHEDULE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_EVENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_BOOT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MAX_INPUT_BYTES = 64 * 1024
_POLL_S = 0.25
_STATE_REL = Path("state/fault-schedules")
_PIDFD_SYSCALLS = {"x86_64": (434, 424), "aarch64": (434, 424)}
_OWNER_FIELDS = {
    "version", "owner_id", "hostname", "boot_id", "pid",
    "process_start_ticks", "acquired_at_unix", "work_root_dev",
    "work_root_ino", "heartbeat_interval_s", "heartbeat_deadline_s",
}


class FaultScheduleError(RuntimeError):
    """Schedule, target authority, or durable evidence is unsafe."""


class TriggerNotObserved(FaultScheduleError):
    """The declared execution did not become uniquely running in time."""


def _bounded_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:500]


def _finite_number(value: Any, *, low: float, high: float) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(float(value)) and low <= float(value) <= high)


def _canonical_dir(value: Any, *, must_exist: bool) -> Path:
    if (not isinstance(value, str) or not value or "\x00" in value
            or not os.path.isabs(value) or os.path.normpath(value) != value):
        raise FaultScheduleError("work_root 须为规范绝对路径")
    path = Path(value)
    try:
        parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise FaultScheduleError("work_root 父目录不可安全解析") from error
    if parent / path.name != path:
        raise FaultScheduleError("work_root 不得经 symlink 转向")
    if path.exists() or os.path.lexists(path):
        info = os.lstat(path)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid()):
            raise FaultScheduleError("work_root 身份非法")
    elif must_exist:
        raise FaultScheduleError("work_root 不存在；先由部署层创建/启动")
    return path


def _read_input(path: Path | str) -> tuple[Dict[str, Any], bytes]:
    name = os.fspath(path)
    if (not isinstance(name, str) or not os.path.isabs(name)
            or os.path.normpath(name) != name or os.path.realpath(name) != name):
        raise FaultScheduleError("schedule path 须为规范绝对路径")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or before.st_nlink != 1 or before.st_mode & 0o022
                    or not 2 <= before.st_size <= _MAX_INPUT_BYTES):
                raise FaultScheduleError("schedule owner/mode/type/size 非法")
            raw = os.pread(fd, before.st_size + 1, 0)
            after = os.fstat(fd)
            if (len(raw) != before.st_size
                    or (before.st_dev, before.st_ino, before.st_size,
                        before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size,
                        after.st_mtime_ns)):
                raise FaultScheduleError("schedule 读取截断/漂移")
        finally:
            os.close(fd)
    except FaultScheduleError:
        raise
    except OSError as error:
        raise FaultScheduleError("schedule 不可安全读取") from error
    try:
        value = _strict_json(raw, label="fault schedule", max_bytes=_MAX_INPUT_BYTES)
    except QualificationFirewallError as error:
        raise FaultScheduleError("schedule 不是严格 JSON") from error
    if raw != _canonical(value):
        raise FaultScheduleError("schedule 须为 canonical JSON + newline")
    return value, raw


def validate_schedule(value: Mapping[str, Any]) -> Dict[str, Any]:
    fields = {
        "version", "protocol", "schedule_id", "work_root",
        "event_timeout_s", "events",
    }
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("version") != 1
            or value.get("protocol") != SCHEDULE_PROTOCOL
            or not isinstance(value.get("schedule_id"), str)
            or _SCHEDULE_ID_RE.fullmatch(value["schedule_id"]) is None
            or not _finite_number(value.get("event_timeout_s"), low=0.1, high=604800)
            or not isinstance(value.get("events"), list)
            or not 1 <= len(value["events"]) <= 128):
        raise FaultScheduleError("fault schedule 顶层字段非法")
    _canonical_dir(value.get("work_root"), must_exist=False)
    seen = set()
    selectors = set()
    events = []
    for index, event in enumerate(value["events"]):
        event_fields = {
            "event_id", "action", "execution_kind",
            "db_owner_kind", "db_owner_id",
        }
        if (not isinstance(event, dict) or set(event) != event_fields
                or not isinstance(event.get("event_id"), str)
                or _EVENT_ID_RE.fullmatch(event["event_id"]) is None
                or event["event_id"] in seen
                or event.get("action") not in {
                    "kill_owner", "kill_execution_payload"}
                or not isinstance(event.get("execution_kind"), str)
                or _KIND_RE.fullmatch(event["execution_kind"]) is None
                or not isinstance(event.get("db_owner_kind"), str)
                or _KIND_RE.fullmatch(event["db_owner_kind"]) is None
                or isinstance(event.get("db_owner_id"), bool)
                or not isinstance(event.get("db_owner_id"), (int, str))
                or (isinstance(event["db_owner_id"], int)
                    and event["db_owner_id"] < 1)
                or (isinstance(event["db_owner_id"], str)
                    and (not event["db_owner_id"]
                         or len(event["db_owner_id"]) > 128
                         or any(ord(ch) < 0x20 or ord(ch) == 0x7f
                                for ch in event["db_owner_id"])))):
            raise FaultScheduleError(f"event[{index}] 字段非法")
        selector = (
            event["execution_kind"], event["db_owner_kind"],
            (type(event["db_owner_id"]).__name__, event["db_owner_id"]),
        )
        if selector in selectors:
            raise FaultScheduleError("同一 execution selector 不得重复消费")
        seen.add(event["event_id"])
        selectors.add(selector)
        events.append(dict(event))
    return {**dict(value), "events": events}


def load_schedule(path: Path | str) -> tuple[Dict[str, Any], bytes]:
    value, raw = _read_input(path)
    return validate_schedule(value), raw


def _ensure_dir(path: Path) -> None:
    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise FaultScheduleError(f"fault state 目录身份非法: {path}")
    if created:
        parent_fd = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def _prepare_state(schedule: Mapping[str, Any], raw: bytes) -> tuple[Path, Path]:
    work = _canonical_dir(schedule["work_root"], must_exist=True)
    state = work / "state"
    base = work / _STATE_REL
    root = base / schedule["schedule_id"]
    for path in (state, base, root, root / "events"):
        _ensure_dir(path)
    schedule_path = root / "schedule.json"
    if os.path.lexists(schedule_path):
        existing = _read_regular(
            schedule_path, label="published schedule",
            expected_owner=os.geteuid(), expected_mode=0o400)
        if existing != raw:
            raise FaultScheduleError("既有 schedule 与输入冲突")
    else:
        try:
            _publish_once(schedule_path, raw, mode=0o400)
        except QualificationFirewallError as error:
            raise FaultScheduleError("schedule 发布失败") from error
    return base, root


def _runner_lock(base: Path) -> int:
    path = base / "runner.lock"
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = -1
    try:
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1):
            raise FaultScheduleError("fault runner lock 身份非法")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException as error:
        if fd >= 0:
            os.close(fd)
        if not isinstance(error, Exception):
            raise
        if isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EAGAIN):
            raise FaultScheduleError("已有 fault runner 正在操作该 work_root") from error
        if isinstance(error, FaultScheduleError):
            raise
        raise FaultScheduleError("fault runner lock 失败") from error


def _load(path: Path, *, label: str) -> tuple[Dict[str, Any], bytes]:
    try:
        raw = _read_regular(
            path, label=label, expected_owner=os.geteuid(), expected_mode=0o400)
        value = _strict_json(raw, label=label)
    except QualificationFirewallError as error:
        raise FaultScheduleError(f"fault receipt 不可安全读取: {label}") from error
    if raw != _canonical(value):
        raise FaultScheduleError(f"fault receipt 非 canonical: {label}")
    return value, raw


def _publish(path: Path, value: Mapping[str, Any]) -> tuple[Dict[str, Any], bytes]:
    try:
        _publish_once(path, _canonical(dict(value)), mode=0o400)
        return _load(path, label=path.name)
    except QualificationFirewallError as error:
        raise FaultScheduleError(f"fault receipt 发布失败: {path.name}") from error


def _event_path(root: Path, event_id: str, kind: str) -> Path:
    return root / "events" / f"{event_id}.{kind}.json"


def _boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii").strip().lower()
    except (OSError, UnicodeError) as error:
        raise FaultScheduleError("boot_id 不可读") from error
    if _BOOT_ID_RE.fullmatch(value) is None:
        raise FaultScheduleError("boot_id 格式非法")
    return value


def _proc_identity(pid: int) -> tuple[str, str]:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = raw.rfind(")")
        rest = raw[closing + 2:].split()
        state, ticks = rest[0], rest[19]
    except (OSError, UnicodeError, IndexError, ValueError) as error:
        raise FaultScheduleError("target /proc identity 不可读") from error
    if closing < 1 or state in {"Z", "X", "x"} or not ticks.isdigit():
        raise FaultScheduleError("target 已退出/zombie 或 start ticks 非法")
    return state, ticks


def _owner_authority(work_root: str) -> Optional[tuple[Dict[str, Any], bytes]]:
    work = Path(work_root)
    path = work / LOCK_NAME
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise FaultScheduleError("instance lock 不可安全打开") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 16 * 1024):
            raise FaultScheduleError("instance lock 身份/大小非法")
        unlocked = False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            unlocked = True
        try:
            visible = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise FaultScheduleError("instance lock pathname 已漂移") from error
        if ((visible.st_dev, visible.st_ino) != (info.st_dev, info.st_ino)
                or stat.S_ISLNK(visible.st_mode)):
            raise FaultScheduleError("instance lock fd/path identity 错配")
        if unlocked:
            return None
        if info.st_size < 2:
            return None  # acquire holds EX before publishing canonical metadata
        raw = os.pread(fd, info.st_size + 1, 0)
        after = os.fstat(fd)
        if (len(raw) != info.st_size
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
            return None  # owner may still be publishing while holding EX
        try:
            owner = _strict_json(raw, label="instance owner", max_bytes=16 * 1024)
        except QualificationFirewallError:
            return None  # partial JSON is not authority and cannot authorize signal
        root_info = os.stat(work, follow_symlinks=False)
        if (raw != _canonical(owner) or set(owner) != _OWNER_FIELDS
                or not _valid_owner_metadata(owner)
                or (owner["work_root_dev"], owner["work_root_ino"])
                != (root_info.st_dev, root_info.st_ino)
                or owner["boot_id"] != _boot_id()):
            raise FaultScheduleError("instance owner authority 绑定非法/非本 boot")
        return owner, raw
    finally:
        os.close(fd)


def _pidfd_open(pid: int) -> int:
    numbers = _PIDFD_SYSCALLS.get(os.uname().machine)
    if numbers is None:
        raise FaultScheduleError("当前架构无受支持 pidfd syscall")
    libc = ctypes.CDLL(None, use_errno=True)
    fd = int(libc.syscall(numbers[0], int(pid), 0))
    if fd < 0:
        code = ctypes.get_errno()
        raise FaultScheduleError(f"pidfd_open 失败: {os.strerror(code)}")
    return fd


def _pidfd_sigkill(fd: int) -> None:
    numbers = _PIDFD_SYSCALLS.get(os.uname().machine)
    if numbers is None:
        raise FaultScheduleError("当前架构无受支持 pidfd syscall")
    libc = ctypes.CDLL(None, use_errno=True)
    if int(libc.syscall(numbers[1], int(fd), int(signal.SIGKILL), 0, 0)) != 0:
        code = ctypes.get_errno()
        raise FaultScheduleError(f"pidfd_send_signal 失败: {os.strerror(code)}")


def _wait_pidfd_exit(fd: int, timeout_s: float) -> None:
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    events = poller.poll(max(1, int(timeout_s * 1000)))
    if not events or not any(mask & select.POLLIN for _fd, mask in events):
        raise FaultScheduleError("pinned owner 未在 deadline 内退出")


def _selector_matches(receipt: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    context = receipt.get("context")
    return (receipt.get("kind") == event["execution_kind"]
            and isinstance(context, dict)
            and context.get("db_owner_kind") == event["db_owner_kind"]
            and context.get("db_owner_id") == event["db_owner_id"])


def _read_execution_json(path: Path) -> tuple[Dict[str, Any], bytes]:
    try:
        raw = _read_regular(
            path, label=path.name, expected_owner=os.geteuid(),
            expected_mode=0o600)
        receipt = _strict_json(raw, label=path.name, max_bytes=128 * 1024)
    except QualificationFirewallError as error:
        raise FaultScheduleError(
            f"execution receipt 不可安全读取: {path.name}") from error
    if raw != _canonical(receipt):
        raise FaultScheduleError(f"execution receipt 非 canonical: {path.name}")
    return receipt, raw


def _read_execution_authority(path: Path) -> tuple[Dict[str, Any], bytes]:
    receipt, raw = _read_execution_json(path)
    try:
        validate_execution_receipt(receipt, path)
    except (OSError, ValueError) as error:
        raise FaultScheduleError(f"execution receipt 损坏: {path.name}") from error
    return receipt, raw


def _declared_target(event: Mapping[str, Any], owner: Mapping[str, Any],
                     receipt: Mapping[str, Any]) -> Dict[str, Any]:
    if event["action"] == "kill_owner":
        pid = owner.get("pid")
        ticks = owner.get("process_start_ticks")
        kind = "instance_owner"
    else:
        pid = receipt.get("payload_pid")
        ticks = receipt.get("payload_start_ticks")
        kind = "execution_payload"
    if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1
            or not isinstance(ticks, str) or not ticks.isascii()
            or not ticks.isdigit()):
        raise FaultScheduleError("signal target identity 非法")
    return {
        "kind": kind, "pid": pid, "process_start_ticks": ticks,
        "boot_id": owner["boot_id"], "owner_id": owner["owner_id"],
        "operation_id": receipt["operation_id"],
    }


def _scan_execution_matches(
        schedule: Mapping[str, Any], event: Mapping[str, Any]) -> list[
            tuple[Path, Dict[str, Any], bytes]]:
    receipt_dir = Path(schedule["work_root"]) / "state/executions"
    matches = []
    if not os.path.lexists(receipt_dir):
        return matches
    info = os.lstat(receipt_dir)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise FaultScheduleError("execution receipt directory identity 非法")
    paths = sorted(receipt_dir.glob("execution-*.json"))
    for path in paths:
        receipt, receipt_raw = _read_execution_json(path)
        if _selector_matches(receipt, event):
            try:
                validate_execution_receipt(receipt, path)
            except (OSError, ValueError) as error:
                raise FaultScheduleError(
                    f"matching execution receipt 损坏: {path.name}") from error
            matches.append((path, receipt, receipt_raw))
    return matches


def _same_execution_match(
        matches: Sequence[tuple[Path, Dict[str, Any], bytes]],
        path: Path, receipt_raw: bytes) -> bool:
    return (len(matches) == 1 and matches[0][0] == path
            and matches[0][2] == receipt_raw
            and matches[0][1].get("state") == "running")


def _wait_target(schedule: Mapping[str, Any], event: Mapping[str, Any]) -> tuple[
        Dict[str, Any], bytes, Path, Dict[str, Any], bytes]:
    deadline = time.monotonic() + float(schedule["event_timeout_s"])
    while time.monotonic() < deadline:
        authority = _owner_authority(schedule["work_root"])
        matches = _scan_execution_matches(schedule, event)
        if len(matches) > 1:
            raise FaultScheduleError("execution selector 在全历史匹配多个 receipt")
        if matches:
            path, receipt, receipt_raw = matches[0]
            if receipt.get("state") == "terminal":
                raise TriggerNotObserved("execution 在 spend 前已 terminal")
            if (receipt.get("state") == "running" and authority is not None
                    and receipt.get("fenced_by_instance_lease") is True):
                owner, owner_raw = authority
                if (receipt.get("owner_id") != owner["owner_id"]
                        or receipt.get("boot_id") != owner["boot_id"]
                        or owner["boot_id"] != _boot_id()):
                    raise FaultScheduleError("execution/lease/boot authority 错配")
                return owner, owner_raw, path, receipt, receipt_raw
        time.sleep(_POLL_S)
    raise TriggerNotObserved("execution trigger 在 deadline 内未出现")


def _pin_target(event: Mapping[str, Any], owner: Mapping[str, Any],
                receipt: Mapping[str, Any]) -> tuple[int, Dict[str, Any]]:
    target = _declared_target(event, owner, receipt)
    pid = target["pid"]
    pidfd = _pidfd_open(pid)
    try:
        _state, observed_ticks = _proc_identity(pid)
        if observed_ticks != target["process_start_ticks"]:
            raise FaultScheduleError("signal target start ticks 漂移")
        return pidfd, target
    except BaseException:
        os.close(pidfd)
        raise


def _wait_owner_gone(schedule: Mapping[str, Any], owner_id: str,
                     receipt_path: Path, target: Mapping[str, Any],
                     *, pidfd: Optional[int]) -> Dict[str, Any]:
    timeout = float(schedule["event_timeout_s"])
    if pidfd is not None:
        _wait_pidfd_exit(pidfd, timeout)
    deadline = time.monotonic() + timeout
    terminal_hash = None
    while time.monotonic() < deadline:
        receipt, receipt_raw = _read_execution_authority(receipt_path)
        if receipt.get("state") == "terminal":
            if (receipt.get("operation_id") != target["operation_id"]
                    or receipt.get("owner_id") != target["owner_id"]
                    or receipt.get("boot_id") != target["boot_id"]
                    or receipt.get("outcome") != "owner_lost"
                    or receipt.get("group_drained") is not True):
                raise FaultScheduleError("owner-loss guardian terminal receipt 错配")
            terminal_hash = _hash_bytes(receipt_raw)
        authority = _owner_authority(schedule["work_root"])
        if (terminal_hash is not None
                and (authority is None or authority[0]["owner_id"] != owner_id)):
            return {
                "kind": ("pinned-owner-exited-guardian-drained"
                         if pidfd is not None
                         else "applied-owner-gone-guardian-drained"),
                "owner_id": owner_id,
                "terminal_receipt_sha256": terminal_hash,
                "terminal_receipt": receipt,
            }
        time.sleep(_POLL_S)
    raise FaultScheduleError("owner exited but delegated fence 未释放")


def _wait_payload_terminal(schedule: Mapping[str, Any], path: Path,
                           target: Mapping[str, Any]) -> Dict[str, Any]:
    deadline = time.monotonic() + float(schedule["event_timeout_s"])
    while time.monotonic() < deadline:
        receipt, receipt_raw = _read_execution_authority(path)
        if receipt.get("state") == "terminal":
            if (receipt.get("operation_id") != target["operation_id"]
                    or receipt.get("owner_id") != target["owner_id"]
                    or receipt.get("boot_id") != target["boot_id"]
                    or receipt.get("payload_pid") != target["pid"]
                    or receipt.get("payload_start_ticks")
                    != target["process_start_ticks"]
                    or receipt.get("outcome") != "exit"
                    or receipt.get("returncode") != -signal.SIGKILL
                    or receipt.get("group_drained") is not True):
                raise FaultScheduleError("payload terminal identity/SIGKILL/drain 错配")
            return {
                "kind": "execution-terminal-sigkill-drained",
                "receipt_sha256": _hash_bytes(receipt_raw),
                "operation_id": receipt["operation_id"],
                "terminal_receipt": receipt,
            }
        time.sleep(_POLL_S)
    raise FaultScheduleError("payload terminal receipt 等待超时")


def _observe_aftermath(
        schedule: Mapping[str, Any], event: Mapping[str, Any],
        spent: Mapping[str, Any], *, pidfd: Optional[int]) -> Dict[str, Any]:
    target = spent["target"]
    receipt_path = Path(schedule["work_root"]) / spent["trigger_receipt_ref"]
    if event["action"] == "kill_owner":
        return _wait_owner_gone(
            schedule, target["owner_id"], receipt_path, target, pidfd=pidfd)
    return _wait_payload_terminal(schedule, receipt_path, target)


def _base(protocol: str, schedule: Mapping[str, Any], schedule_hash: str,
          event: Mapping[str, Any], index: int) -> Dict[str, Any]:
    return {
        "version": 1, "protocol": protocol,
        "schedule_id": schedule["schedule_id"],
        "schedule_sha256": schedule_hash,
        "event_index": index, "event_id": event["event_id"],
        "action": event["action"],
    }


def _result(schedule: Mapping[str, Any], schedule_hash: str,
            event: Mapping[str, Any], index: int, *, status: str,
            reason: str, spent_hash: Optional[str],
            applied_hash: Optional[str]) -> Dict[str, Any]:
    return {
        **_base(RESULT_PROTOCOL, schedule, schedule_hash, event, index),
        "status": status, "reason": reason[:500],
        "spent_sha256": spent_hash, "applied_sha256": applied_hash,
        "signal_exactly_once": False,
    }


def _validate_chain(root: Path, schedule: Mapping[str, Any], schedule_hash: str,
                    event: Mapping[str, Any], index: int) -> tuple[
                        Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    spent_path = _event_path(root, event["event_id"], "spent")
    applied_path = _event_path(root, event["event_id"], "applied")
    spent = applied = None
    spent_hash = applied_hash = None
    if os.path.lexists(spent_path):
        spent, spent_raw = _load(spent_path, label=spent_path.name)
        expected = _base(SPENT_PROTOCOL, schedule, schedule_hash, event, index)
        fields = set(expected) | {
            "owner_metadata_sha256", "owner_metadata",
            "trigger_receipt_ref", "trigger_receipt_sha256",
            "trigger_receipt", "target", "spent_at_unix",
        }
        owner = spent.get("owner_metadata")
        receipt = spent.get("trigger_receipt")
        if (set(spent) != fields
                or any(spent.get(key) != item for key, item in expected.items())
                or not isinstance(owner, dict) or set(owner) != _OWNER_FIELDS
                or not _valid_owner_metadata(owner)
                or owner.get("boot_id") is None
                or _BOOT_ID_RE.fullmatch(owner["boot_id"]) is None
                or not isinstance(receipt, dict)
                or receipt.get("state") != "running"
                or receipt.get("fenced_by_instance_lease") is not True
                or receipt.get("owner_id") != owner.get("owner_id")
                or receipt.get("boot_id") != owner.get("boot_id")
                or not _selector_matches(receipt, event)
                or spent.get("trigger_receipt_ref")
                != f"state/executions/execution-{receipt.get('operation_id')}.json"
                or spent.get("owner_metadata_sha256")
                != _hash_bytes(_canonical(owner))
                or spent.get("trigger_receipt_sha256")
                != _hash_bytes(_canonical(receipt))
                or spent.get("target") != _declared_target(event, owner, receipt)
                or not _finite_number(
                    spent.get("spent_at_unix"), low=1.0, high=1e20)):
            raise FaultScheduleError("spent receipt 绑定非法")
        work_info = os.stat(schedule["work_root"], follow_symlinks=False)
        if ((owner["work_root_dev"], owner["work_root_ino"])
                != (work_info.st_dev, work_info.st_ino)):
            raise FaultScheduleError("spent owner/work-root identity 错配")
        receipt_path = Path(schedule["work_root"]) / spent["trigger_receipt_ref"]
        try:
            validate_execution_receipt(receipt, receipt_path)
        except (OSError, ValueError) as error:
            raise FaultScheduleError("spent frozen execution receipt 非法") from error
        spent_hash = _hash_bytes(spent_raw)
    if os.path.lexists(applied_path):
        if spent is None:
            raise FaultScheduleError("applied 存在但 spent 缺失")
        applied, applied_raw = _load(applied_path, label=applied_path.name)
        expected = _base(APPLIED_PROTOCOL, schedule, schedule_hash, event, index)
        fields = set(expected) | {
            "spent_sha256", "target", "signal", "send_result",
            "signal_exactly_once", "applied_at_unix",
        }
        if (set(applied) != fields
                or any(applied.get(key) != item for key, item in expected.items())
                or applied.get("spent_sha256") != spent_hash
                or applied.get("target") != spent.get("target")
                or applied.get("signal") != "SIGKILL"
                or applied.get("send_result") != "pidfd_kernel_accepted"
                or applied.get("signal_exactly_once") is not False
                or not _finite_number(
                    applied.get("applied_at_unix"), low=1.0, high=1e20)):
            raise FaultScheduleError("applied receipt 绑定非法")
        applied_hash = _hash_bytes(applied_raw)
    return spent, spent_hash, applied_hash


def _validate_observed_evidence(
        schedule: Mapping[str, Any], event: Mapping[str, Any],
        spent: Mapping[str, Any], evidence: Mapping[str, Any]) -> None:
    target = spent["target"]
    terminal = evidence.get("terminal_receipt")
    if not isinstance(terminal, dict):
        raise FaultScheduleError("observed evidence 缺 terminal receipt")
    receipt_path = (
        Path(schedule["work_root"]) / spent["trigger_receipt_ref"])
    try:
        validate_execution_receipt(terminal, receipt_path)
    except (OSError, ValueError) as error:
        raise FaultScheduleError("observed terminal receipt 非法") from error
    common = (
        terminal.get("state") == "terminal"
        and terminal.get("operation_id") == target["operation_id"]
        and terminal.get("owner_id") == target["owner_id"]
        and terminal.get("boot_id") == target["boot_id"]
        and terminal.get("group_drained") is True)
    if event["action"] == "kill_owner":
        fields = {
            "kind", "owner_id", "terminal_receipt_sha256",
            "terminal_receipt",
        }
        valid = (
            set(evidence) == fields
            and evidence.get("kind") in {
                "pinned-owner-exited-guardian-drained",
                "applied-owner-gone-guardian-drained",
            }
            and evidence.get("owner_id") == target["owner_id"]
            and evidence.get("terminal_receipt_sha256")
            == _hash_bytes(_canonical(terminal))
            and common and terminal.get("outcome") == "owner_lost")
    else:
        fields = {
            "kind", "receipt_sha256", "operation_id", "terminal_receipt",
        }
        valid = (
            set(evidence) == fields
            and evidence.get("kind") == "execution-terminal-sigkill-drained"
            and evidence.get("operation_id") == target["operation_id"]
            and evidence.get("receipt_sha256")
            == _hash_bytes(_canonical(terminal))
            and common and terminal.get("outcome") == "exit"
            and terminal.get("payload_pid") == target["pid"]
            and terminal.get("payload_start_ticks")
            == target["process_start_ticks"]
            and terminal.get("returncode") == -signal.SIGKILL)
    if not valid:
        raise FaultScheduleError("observed aftermath evidence 绑定非法")


def _existing_result(root: Path, schedule: Mapping[str, Any], schedule_hash: str,
                     event: Mapping[str, Any], index: int) -> Optional[Dict[str, Any]]:
    path = _event_path(root, event["event_id"], "result")
    if not os.path.lexists(path):
        return None
    value, _raw = _load(path, label=path.name)
    expected = _base(RESULT_PROTOCOL, schedule, schedule_hash, event, index)
    expected_fields = set(expected) | {
        "status", "reason", "spent_sha256", "applied_sha256",
        "signal_exactly_once", "evidence",
    }
    spent, spent_hash, applied_hash = _validate_chain(
        root, schedule, schedule_hash, event, index)
    if (set(value) != expected_fields
            or any(value.get(key) != item for key, item in expected.items())
            or value.get("status") not in {"observed", "failed", "inconclusive"}
            or not isinstance(value.get("reason"), str)
            or not 1 <= len(value["reason"]) <= 500
            or value.get("signal_exactly_once") is not False
            or value.get("spent_sha256") != spent_hash
            or value.get("applied_sha256") != applied_hash
            or (value["status"] == "observed"
                and (spent is None or applied_hash is None
                     or not isinstance(value.get("evidence"), dict)))
            or (value["status"] != "observed" and value.get("evidence") is not None)
            or (value["status"] == "failed"
                and (spent_hash is not None or applied_hash is not None))
            or (value["status"] == "inconclusive" and spent_hash is None)):
        raise FaultScheduleError("event result 绑定非法")
    if value["status"] == "observed":
        _validate_observed_evidence(schedule, event, spent, value["evidence"])
    return value


def _run_event(root: Path, schedule: Mapping[str, Any], schedule_hash: str,
               event: Mapping[str, Any], index: int) -> Dict[str, Any]:
    existing = _existing_result(root, schedule, schedule_hash, event, index)
    if existing is not None:
        return existing
    spent_path = _event_path(root, event["event_id"], "spent")
    applied_path = _event_path(root, event["event_id"], "applied")
    if os.path.lexists(spent_path) or os.path.lexists(applied_path):
        spent, spent_hash, applied_hash = _validate_chain(
            root, schedule, schedule_hash, event, index)
        if applied_hash is None:
            result = {
                **_result(
                    schedule, schedule_hash, event, index,
                    status="inconclusive",
                    reason="delivery_unproven_after_runner_crash; not replayed",
                    spent_hash=spent_hash, applied_hash=None),
                "evidence": None,
            }
        else:
            try:
                evidence = _observe_aftermath(
                    schedule, event, spent, pidfd=None)
                result = {
                    **_result(
                        schedule, schedule_hash, event, index,
                        status="observed",
                        reason="accepted SIGKILL aftermath observed after restart",
                        spent_hash=spent_hash, applied_hash=applied_hash),
                    "evidence": evidence,
                }
            except Exception as error:
                result = {
                    **_result(
                        schedule, schedule_hash, event, index,
                        status="inconclusive", reason=_bounded_error(error),
                        spent_hash=spent_hash, applied_hash=applied_hash),
                    "evidence": None,
                }
        return _publish(
            _event_path(root, event["event_id"], "result"), result)[0]

    pidfd = -1
    spent_hash = applied_hash = None
    try:
        owner, owner_raw, receipt_path, receipt, receipt_raw = _wait_target(
            schedule, event)
        pidfd, target = _pin_target(event, owner, receipt)
        # Close the authority read→signal race: owner metadata and target must
        # still match while pidfd pins the exact task.
        current = _owner_authority(schedule["work_root"])
        if current is None or current[1] != owner_raw:
            raise FaultScheduleError("owner generation 在 spend 前漂移")
        matches = _scan_execution_matches(schedule, event)
        if not _same_execution_match(matches, receipt_path, receipt_raw):
            raise FaultScheduleError("execution authority 在 spend 前漂移")
        _state, current_ticks = _proc_identity(target["pid"])
        if current_ticks != target["process_start_ticks"]:
            raise FaultScheduleError("signal target 在 spend 前漂移")
        spent = {
            **_base(SPENT_PROTOCOL, schedule, schedule_hash, event, index),
            "owner_metadata_sha256": _hash_bytes(owner_raw),
            "owner_metadata": owner,
            "trigger_receipt_ref": f"state/executions/{receipt_path.name}",
            "trigger_receipt_sha256": _hash_bytes(receipt_raw),
            "trigger_receipt": receipt,
            "target": target, "spent_at_unix": time.time(),
        }
        _spent, spent_raw = _publish(spent_path, spent)
        spent_hash = _hash_bytes(spent_raw)
        if not _same_execution_match(
                _scan_execution_matches(schedule, event),
                receipt_path, receipt_raw):
            raise FaultScheduleError(
                "execution selector 在 signal 前变得歧义/漂移")
        _pidfd_sigkill(pidfd)
        applied = {
            **_base(APPLIED_PROTOCOL, schedule, schedule_hash, event, index),
            "spent_sha256": spent_hash, "target": target,
            "signal": "SIGKILL", "send_result": "pidfd_kernel_accepted",
            "signal_exactly_once": False, "applied_at_unix": time.time(),
        }
        _applied, applied_raw = _publish(applied_path, applied)
        applied_hash = _hash_bytes(applied_raw)
        evidence = _observe_aftermath(
            schedule, event, spent, pidfd=pidfd)
        result = {
            **_result(
                schedule, schedule_hash, event, index, status="observed",
                reason="target exit consistent with accepted SIGKILL",
                spent_hash=spent_hash, applied_hash=applied_hash),
            "evidence": evidence,
        }
    except TriggerNotObserved as error:
        result = {
            **_result(
                schedule, schedule_hash, event, index, status="failed",
                reason=_bounded_error(error), spent_hash=None, applied_hash=None),
            "evidence": None,
        }
    except Exception as error:
        durable_spent = None
        if os.path.lexists(spent_path) or os.path.lexists(applied_path):
            durable_spent, spent_hash, applied_hash = _validate_chain(
                root, schedule, schedule_hash, event, index)
        if applied_hash is not None:
            try:
                evidence = _observe_aftermath(
                    schedule, event, durable_spent,
                    pidfd=(pidfd if pidfd >= 0 else None))
                result = {
                    **_result(
                        schedule, schedule_hash, event, index,
                        status="observed",
                        reason="durable applied SIGKILL aftermath observed",
                        spent_hash=spent_hash, applied_hash=applied_hash),
                    "evidence": evidence,
                }
            except Exception as aftermath_error:
                result = {
                    **_result(
                        schedule, schedule_hash, event, index,
                        status="inconclusive",
                        reason=_bounded_error(aftermath_error),
                        spent_hash=spent_hash, applied_hash=applied_hash),
                    "evidence": None,
                }
        else:
            result = {
                **_result(
                    schedule, schedule_hash, event, index,
                    status=("inconclusive"
                            if spent_hash is not None else "failed"),
                    reason=_bounded_error(error), spent_hash=spent_hash,
                    applied_hash=None),
                "evidence": None,
            }
    finally:
        if pidfd >= 0:
            os.close(pidfd)
    return _publish(_event_path(root, event["event_id"], "result"), result)[0]


def _final(schedule: Mapping[str, Any], schedule_hash: str,
           results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    complete = len(results) == len(schedule["events"])
    observed = complete and all(item["status"] == "observed" for item in results)
    if observed:
        status, reason = "complete", "all scheduled fault aftermaths observed"
    elif any(item["status"] == "inconclusive" for item in results):
        status, reason = "inconclusive", "at least one delivery gap is unprovable"
    else:
        status, reason = "failed", "schedule stopped at first failed event"
    return {
        "version": 1, "protocol": FINAL_PROTOCOL,
        "schedule_id": schedule["schedule_id"],
        "schedule_sha256": schedule_hash, "status": status, "reason": reason,
        "event_result_sha256": [
            _hash_bytes(_canonical(dict(item))) for item in results],
        "recovery_verified": False, "signal_exactly_once": False,
    }


def run_fault_schedule(schedule_path: Path | str) -> Dict[str, Any]:
    schedule, raw = load_schedule(schedule_path)
    schedule_hash = _hash_bytes(raw)
    base, root = _prepare_state(schedule, raw)
    lock_fd = _runner_lock(base)
    try:
        final_path = root / "final.json"
        verified = verify_fault_schedule(schedule_path)
        if os.path.lexists(final_path):
            return verified
        results = []
        for index, event in enumerate(schedule["events"]):
            result = _run_event(root, schedule, schedule_hash, event, index)
            results.append(result)
            if result["status"] != "observed":
                break
        _publish(final_path, _final(schedule, schedule_hash, results))
        return verify_fault_schedule(schedule_path)
    finally:
        os.close(lock_fd)


def _validate_state_layout(
        schedule: Mapping[str, Any], root: Path, *, allowed_through: int) -> None:
    work = Path(schedule["work_root"])
    for path in (work / "state", work / _STATE_REL, root, root / "events"):
        try:
            info = os.lstat(path)
        except OSError as error:
            raise FaultScheduleError("fault state directory 缺失/漂移") from error
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise FaultScheduleError("fault state directory identity 非法")
    root_names = set(os.listdir(root))
    if not root_names <= {"schedule.json", "events", "final.json"}:
        raise FaultScheduleError("fault schedule root 含未知文件")
    event_names = set(os.listdir(root / "events"))
    known = {
        f"{event['event_id']}.{kind}.json"
        for event in schedule["events"]
        for kind in ("spent", "applied", "result")
    }
    if not event_names <= known:
        raise FaultScheduleError("fault event directory 含未知文件")
    for index, event in enumerate(schedule["events"]):
        if index <= allowed_through:
            continue
        if any(
                f"{event['event_id']}.{kind}.json" in event_names
                for kind in ("spent", "applied", "result")):
            raise FaultScheduleError("fault event 文件越过线性终止前缀")


def verify_fault_schedule(schedule_path: Path | str) -> Dict[str, Any]:
    schedule, raw = load_schedule(schedule_path)
    schedule_hash = _hash_bytes(raw)
    root = Path(schedule["work_root"]) / _STATE_REL / schedule["schedule_id"]
    if not os.path.lexists(root):
        return {
            "version": 1, "protocol": FINAL_PROTOCOL,
            "schedule_id": schedule["schedule_id"],
            "schedule_sha256": schedule_hash, "status": "incomplete",
            "reason": "fault schedule state absent", "event_result_sha256": [],
            "recovery_verified": False, "signal_exactly_once": False,
        }
    root_info = os.lstat(root)
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise FaultScheduleError("fault schedule state path 非目录")
    if not os.path.lexists(root / "schedule.json"):
        _validate_state_layout(schedule, root, allowed_through=-1)
        if set(os.listdir(root)) != {"events"}:
            raise FaultScheduleError(
                "published schedule 缺失但已有其他 fault 制品")
        return {
            "version": 1, "protocol": FINAL_PROTOCOL,
            "schedule_id": schedule["schedule_id"],
            "schedule_sha256": schedule_hash, "status": "incomplete",
            "reason": "published schedule missing", "event_result_sha256": [],
            "recovery_verified": False, "signal_exactly_once": False,
        }
    published, published_raw = _load(root / "schedule.json", label="published schedule")
    if published != schedule or published_raw != raw:
        raise FaultScheduleError("published schedule 与输入冲突")
    results = []
    partial_index = None
    stopped_index = None
    for index, event in enumerate(schedule["events"]):
        result = _existing_result(root, schedule, schedule_hash, event, index)
        if result is None:
            _validate_chain(root, schedule, schedule_hash, event, index)
            partial_index = index
            break
        results.append(result)
        if result["status"] != "observed":
            stopped_index = index
            break
    allowed_through = (
        partial_index if partial_index is not None
        else stopped_index if stopped_index is not None
        else len(schedule["events"]) - 1)
    final_path = root / "final.json"
    if not os.path.lexists(final_path):
        _validate_state_layout(
            schedule, root, allowed_through=allowed_through)
        return {
            "version": 1, "protocol": FINAL_PROTOCOL,
            "schedule_id": schedule["schedule_id"],
            "schedule_sha256": schedule_hash, "status": "incomplete",
            "reason": "event result/final missing",
            "event_result_sha256": [
                _hash_bytes(_canonical(dict(item))) for item in results],
            "recovery_verified": False, "signal_exactly_once": False,
        }
    if partial_index is not None:
        raise FaultScheduleError("fault final 越过缺失 event result")
    final, _raw_final = _load(final_path, label="fault final")
    expected = _final(schedule, schedule_hash, results)
    if final != expected:
        raise FaultScheduleError("fault final 与线性 result prefix 不一致")
    _validate_state_layout(schedule, root, allowed_through=allowed_through)
    return final


def _print(value: Mapping[str, Any], *, stream=None) -> None:
    print(json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False), file=stream)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="meta-research fixed linear fault sidecar")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "run", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--schedule", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            schedule, raw = load_schedule(args.schedule)
            result = {
                "status": "valid", "protocol": SCHEDULE_PROTOCOL,
                "schedule_id": schedule["schedule_id"],
                "schedule_sha256": _hash_bytes(raw),
                "event_count": len(schedule["events"]),
            }
        elif args.command == "run":
            result = run_fault_schedule(args.schedule)
        else:
            result = verify_fault_schedule(args.schedule)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        _print({"status": "unsafe", "error": _bounded_error(error)}, stream=sys.stderr)
        return 3
    _print(result)
    if result.get("status") in {"incomplete", "inconclusive"}:
        return 2
    return 0 if result.get("status") in {"valid", "complete"} else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FaultScheduleError", "load_schedule", "validate_schedule",
    "run_fault_schedule", "verify_fault_schedule", "main",
]
