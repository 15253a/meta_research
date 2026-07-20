"""Owner-death-aware supervision for every external execution.

The orchestrator process is not a sufficient lifecycle authority: SIGKILL
cannot run its ``finally`` blocks.  Each execution is therefore owned by a
small external guardian which:

* holds a delegated duplicate of the instance flock (when assembled by
  :func:`build_system`),
* observes an owner-only pipe for EOF,
* becomes a Linux child subreaper,
* starts the workload in a fresh session behind a durable-start barrier,
* terminates and reaps the complete trusted descendant tree, and
* publishes a fsync'd terminal receipt before releasing the flock duplicate.

For ordinary calls this closes same-host overlap for trusted workloads,
including ``setsid``/double-fork daemons, but is not by itself an adversarial
sandbox.  Sandboxed calls additionally delegate an exact random Docker
name+label capability: terminal publication waits until both the local tree and
daemon-owned container are proven absent.  Filesystem/network/privilege policy
is enforced by :mod:`execution_sandbox`; the guardian owns lifecycle proof.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


_HELPER_FLAG = "execution-guardian"
_PAYLOAD_FLAG = "execution-payload"
_RECEIPT_VERSION = 1
_MAX_SPEC_BYTES = 2 * 1024 * 1024
_MAX_RECEIPT_BYTES = 128 * 1024
_HEARTBEAT_INTERVAL_S = 2.0
_ACTIVITY_SAMPLE_INTERVAL_S = 0.5
_KIND_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_OPERATION_RE = re.compile(r"^exec-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_NAME_RE = re.compile(r"^mr-[a-z0-9][a-z0-9_.-]{0,61}$")
_SANDBOX_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_SANDBOX_LABEL = "meta-research.sandbox-token"
_SANDBOX_BACKEND = "docker-container-v1"
_SANDBOX_RESOURCE_MODES = frozenset({"cgroup-v1", "cgroup-v2", "rlimit-fallback"})
_TERMINAL_OUTCOMES = frozenset({
    "exit", "timeout", "cancelled", "owner_lost", "spawn_failed",
    "lingering_descendant", "owner_lost_before_start",
})


class ExecutionSupervisorError(RuntimeError):
    """The guardian protocol or its durable recovery state is unsafe."""


class ExecutionRecoveryError(ExecutionSupervisorError):
    """A prior execution cannot be proven empty; new work must fail closed."""


class ExecutionCancelled(ExecutionSupervisorError):
    def __init__(self, message: str, *, receipt: Dict[str, Any], receipt_path: Path):
        super().__init__(message)
        self.receipt = receipt
        self.receipt_path = receipt_path


class ExecutionCleanupError(ExecutionSupervisorError):
    def __init__(self, message: str, *, receipt: Dict[str, Any], receipt_path: Path):
        super().__init__(message)
        self.receipt = receipt
        self.receipt_path = receipt_path


class SupervisedTimeoutExpired(subprocess.TimeoutExpired):
    def __init__(self, cmd, timeout, *, output, stderr,
                 receipt: Dict[str, Any], receipt_path: Path):  # noqa: ANN001
        super().__init__(cmd, timeout, output=output, stderr=stderr)
        self.receipt = receipt
        self.receipt_path = receipt_path


@dataclass
class ExecutionResult:
    args: Sequence[str]
    returncode: int
    stdout: Optional[bytes]
    stderr: Optional[bytes]
    receipt: Dict[str, Any]
    receipt_path: Path
    heartbeat_path: Optional[Path] = None


@dataclass
class _ActiveExecution:
    operation_id: str
    helper: subprocess.Popen
    owner_write_fd: int

    def cancel(self) -> None:
        if self.owner_write_fd < 0:
            return
        try:
            os.write(self.owner_write_fd, b"C")
        except (BrokenPipeError, BlockingIOError, OSError):
            pass

    def close_owner_pipe(self) -> None:
        fd, self.owner_write_fd = self.owner_write_fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


_GLOBAL_GUARD = threading.RLock()
_GLOBAL_CONDITION = threading.Condition(_GLOBAL_GUARD)
_GLOBAL_ACTIVE: Dict[str, _ActiveExecution] = {}
_GLOBAL_HARD_STOP = False


def _after_fork_child() -> None:
    """A fork child must not keep the owner's death-pipe write ends alive."""
    global _GLOBAL_GUARD, _GLOBAL_CONDITION, _GLOBAL_ACTIVE, _GLOBAL_HARD_STOP
    for active in list(_GLOBAL_ACTIVE.values()):
        active.close_owner_pipe()
    _GLOBAL_ACTIVE = {}
    _GLOBAL_HARD_STOP = True
    _GLOBAL_GUARD = threading.RLock()
    _GLOBAL_CONDITION = threading.Condition(_GLOBAL_GUARD)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


def terminate_all_supervised_executions(*, wait_s: float = 5.0) -> None:
    """Permanently reject new executions and request cleanup of every guardian.

    Guardians, not this caller, own TERM/KILL/reap.  Waiting observes registry
    convergence only and never races another thread for helper stdout/stderr.
    """
    global _GLOBAL_HARD_STOP
    if (isinstance(wait_s, bool) or not isinstance(wait_s, (int, float))
            or not math.isfinite(float(wait_s)) or float(wait_s) < 0):
        raise ValueError("wait_s 须为非负有限数")
    with _GLOBAL_CONDITION:
        _GLOBAL_HARD_STOP = True
        for active in list(_GLOBAL_ACTIVE.values()):
            active.cancel()
        deadline = time.monotonic() + float(wait_s)
        while _GLOBAL_ACTIVE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExecutionSupervisorError(
                    f"{len(_GLOBAL_ACTIVE)} 个 guardian 未在 hard-stop deadline 内收敛")
            _GLOBAL_CONDITION.wait(min(0.1, remaining))


def _reset_global_hard_stop_for_tests() -> None:
    """Test-only reset; a production hard-stop is intentionally irreversible."""
    global _GLOBAL_HARD_STOP
    with _GLOBAL_CONDITION:
        if _GLOBAL_ACTIVE:
            raise RuntimeError("仍有 active execution，不得复位 hard-stop")
        _GLOBAL_HARD_STOP = False


def _strict_json_bytes(raw: bytes, *, limit: int) -> Dict[str, Any]:
    if len(raw) > limit:
        raise ValueError("JSON 超过大小上限")

    def unique_object(pairs):  # noqa: ANN001
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"重复 JSON key: {key}")
            value[key] = item
        return value

    value = json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique_object,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"非有限 JSON number: {token}")))
    if not isinstance(value, dict):
        raise ValueError("JSON 须为 object")
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False) + "\n").encode("utf-8")


def _executable_identity(path: str) -> str:
    """Hash one trusted host executable without following its final component."""
    if (not isinstance(path, str) or not path or "\x00" in path
            or not os.path.isabs(path) or os.path.normpath(path) != path):
        raise ValueError("sandbox engine_path 须为规范绝对路径")
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o022 or not info.st_mode & 0o111):
            raise PermissionError("sandbox engine 须为 root/owner 持有、不可组/全局写的可执行常规文件")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(fd)


def _normalize_external_container(value: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate the narrow external-container cleanup capability passed to a guardian."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("external_container 须为 mapping 或 None")
    expected_keys = {
        "backend", "engine_path", "engine_host", "container_name", "token", "spec_sha256",
        "network_mode", "rootfs_readonly", "no_new_privileges", "cap_drop_all",
        "pid_namespace", "resource_mode",
    }
    local_keys = {
        "local_environment_identity_sha256",
        "local_environment_development_only",
    }
    network_keys = {"network_development_only"}
    allowed = set(expected_keys)
    if local_keys & set(value):
        allowed |= local_keys
    if network_keys & set(value):
        allowed |= network_keys
    if set(value) != allowed:
        raise ValueError("external_container 字段闭包非法")
    if value.get("backend") != _SANDBOX_BACKEND:
        raise ValueError("external_container backend 非法")
    engine_host = value.get("engine_host")
    if (not isinstance(engine_host, str) or not engine_host.startswith("unix:///")
            or "\x00" in engine_host
            or os.path.normpath(engine_host.removeprefix("unix://"))
            != engine_host.removeprefix("unix://")):
        raise ValueError("external_container engine_host 只接受规范绝对 unix socket")
    name, token = value.get("container_name"), value.get("token")
    if (not isinstance(name, str) or _CONTAINER_NAME_RE.fullmatch(name) is None
            or not isinstance(token, str) or _SANDBOX_TOKEN_RE.fullmatch(token) is None):
        raise ValueError("external_container name/token 非法")
    spec_sha256 = value.get("spec_sha256")
    if not isinstance(spec_sha256, str) or _SHA256_RE.fullmatch(spec_sha256) is None:
        raise ValueError("external_container spec_sha256 非法")
    network_mode = value.get("network_mode")
    if network_mode not in {"none", "bridge"}:
        raise ValueError("external_container network_mode 非法")
    if ((network_mode == "bridge" and value.get("network_development_only") is not True)
            or (network_mode == "none" and network_keys & set(value))):
        raise ValueError("external_container bridge 必须显式 development-only")
    for field in ("rootfs_readonly", "no_new_privileges", "cap_drop_all", "pid_namespace"):
        if value.get(field) is not True:
            raise ValueError(f"external_container {field} 必须为 true")
    resource_mode = value.get("resource_mode")
    if resource_mode not in _SANDBOX_RESOURCE_MODES:
        raise ValueError("external_container resource_mode 非法")
    local_identity = value.get("local_environment_identity_sha256")
    if local_keys <= set(value) and (
            not isinstance(local_identity, str)
            or _SHA256_RE.fullmatch(local_identity) is None
            or value.get("local_environment_development_only") is not True):
        raise ValueError("external_container local environment identity 非法")
    raw_engine_path = value.get("engine_path")
    if not isinstance(raw_engine_path, str) or not os.path.isabs(raw_engine_path):
        raise ValueError("external_container engine_path 须为绝对路径")
    engine_path = os.path.realpath(raw_engine_path)
    engine_sha256 = _executable_identity(engine_path)
    normalized = {
        "backend": _SANDBOX_BACKEND,
        "engine_path": engine_path,
        "engine_host": engine_host,
        "engine_sha256": engine_sha256,
        "container_name": name,
        "token": token,
        "spec_sha256": spec_sha256,
        "network_mode": network_mode,
        "rootfs_readonly": True,
        "no_new_privileges": True,
        "cap_drop_all": True,
        "pid_namespace": True,
        "resource_mode": resource_mode,
    }
    if local_keys <= set(value):
        normalized.update({
            "local_environment_identity_sha256": local_identity,
            "local_environment_development_only": True,
        })
    if network_mode == "bridge":
        normalized["network_development_only"] = True
    return normalized


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def atomic_write_receipt(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically publish one private receipt and fsync its directory."""
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = _canonical_json(value)
    if len(payload) > _MAX_RECEIPT_BYTES:
        raise ValueError("execution receipt 超过大小上限")
    tmp_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    tmp_fd = -1
    try:
        parent_info = os.fstat(dir_fd)
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid():
            raise PermissionError("execution receipt 目录身份非法")
        os.fchmod(dir_fd, 0o700)
        tmp_fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
        _write_all(tmp_fd, payload)
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = -1
        os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        if tmp_fd >= 0:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(dir_fd)


def _atomic_write_heartbeat(path: Path, value: Mapping[str, Any]) -> None:
    """Publish a non-authoritative heartbeat snapshot without per-tick directory fsync."""
    path = Path(path)
    parent = path.parent
    payload = _canonical_json(value)
    if len(payload) > _MAX_RECEIPT_BYTES:
        raise ValueError("execution heartbeat 超过大小上限")
    tmp_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    tmp_fd = -1
    try:
        info = os.fstat(dir_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError("execution heartbeat 目录身份非法")
        tmp_fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
        _write_all(tmp_fd, payload)
        os.close(tmp_fd)
        tmp_fd = -1
        os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        if tmp_fd >= 0:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(dir_fd)


def read_receipt(path: Path) -> Dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    dir_fd = os.open(
        Path(path).parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        parent_info = os.fstat(dir_fd)
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid():
            raise ValueError("execution receipt 目录身份非法")
        fd = os.open(Path(path).name, flags, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size < 2 or info.st_size > _MAX_RECEIPT_BYTES):
            raise ValueError("execution receipt 文件身份/大小/权限非法")
        chunks, remaining = [], info.st_size
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                raise ValueError("execution receipt 被截断")
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    return _strict_json_bytes(b"".join(chunks), limit=_MAX_RECEIPT_BYTES)


def _capture_path(path: Path, operation_id: str, stream: str) -> Path:
    return Path(path).parent / f"capture-{operation_id}.{stream}.bin"


def _open_capture_file(path: Path):  # noqa: ANN201 - binary file object
    flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "w+b")
    except BaseException:
        os.close(fd)
        raise


def _capture_identity(path: Path, fd: int) -> Dict[str, Any]:
    """Hash one already-open guardian output without disturbing its file offset."""
    os.fsync(fd)
    info = os.fstat(fd)
    current = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
            or (current.st_dev, current.st_ino, current.st_size)
            != (info.st_dev, info.st_ino, info.st_size)):
        raise ExecutionSupervisorError(f"durable capture 身份非法: {path.name}")
    digest = hashlib.sha256()
    offset = 0
    while offset < info.st_size:
        chunk = os.pread(fd, min(1024 * 1024, info.st_size - offset), offset)
        if not chunk:
            raise ExecutionSupervisorError(f"durable capture 被截断: {path.name}")
        digest.update(chunk)
        offset += len(chunk)
    return {
        "sha256": "sha256:" + digest.hexdigest(),
        "bytes": info.st_size,
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def _capture_identity_from_path(path: Path) -> Dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
    try:
        return _capture_identity(path, fd)
    finally:
        os.close(fd)


def _verified_capture_fd(receipt: Mapping[str, Any], *, stream: str) -> Tuple[int, Dict[str, Any]]:
    if stream not in ("stdout", "stderr"):
        raise ValueError("capture stream 只接受 stdout/stderr")
    ref = receipt.get(f"capture_{stream}_ref")
    if not isinstance(ref, str) or not ref:
        raise ValueError(f"execution receipt 缺 capture_{stream}_ref")
    path = Path(ref)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        identity = _capture_identity(path, fd)
        for field in ("sha256", "bytes", "device", "inode"):
            if receipt.get(f"capture_{stream}_{field}") != identity[field]:
                raise ValueError(f"execution {stream} capture 与 receipt 身份不一致")
        return fd, identity
    except BaseException:
        os.close(fd)
        raise


def read_execution_capture(receipt: Mapping[str, Any], *, stream: str) -> bytes:
    """Read a terminal guardian capture from one no-follow descriptor and verify its receipt identity."""
    if receipt.get("capture_error") is not None:
        raise ValueError("execution receipt 已声明 capture_error，输出不可作为 usage authority")
    fd, identity = _verified_capture_fd(receipt, stream=stream)
    try:
        remaining = identity["bytes"]
        chunks = []
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"execution {stream} capture 读取被截断")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _finite_number(value: Any, *, positive: bool = False) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and (not positive or float(value) > 0))


def _validate_sandbox_receipt(
        sandbox: Any, *, containment: Any, terminal: bool) -> None:
    if containment == "trusted-descendant-tree":
        if sandbox is not None:
            raise ValueError("trusted execution 不得携 sandbox authority")
        return
    if containment != _SANDBOX_BACKEND or not isinstance(sandbox, dict):
        raise ValueError("sandbox containment/authority 非法")
    common = {
        "backend", "engine_path", "engine_host", "engine_sha256", "container_name", "token",
        "spec_sha256", "network_mode", "rootfs_readonly", "no_new_privileges",
        "cap_drop_all", "pid_namespace", "resource_mode",
    }
    local_keys = {
        "local_environment_identity_sha256",
        "local_environment_development_only",
    }
    network_keys = {"network_development_only"}
    has_local = bool(local_keys & set(sandbox))
    has_network_development = bool(network_keys & set(sandbox))
    expected = (common | (local_keys if has_local else set())
                | (network_keys if has_network_development else set())
                | ({"container_drained"} if terminal else set()))
    if set(sandbox) != expected:
        raise ValueError("sandbox receipt 字段闭包非法")
    if (sandbox.get("backend") != _SANDBOX_BACKEND
            or not isinstance(sandbox.get("engine_path"), str)
            or not os.path.isabs(sandbox["engine_path"])
            or os.path.normpath(sandbox["engine_path"]) != sandbox["engine_path"]
            or not isinstance(sandbox.get("engine_host"), str)
            or not sandbox["engine_host"].startswith("unix:///")
            or os.path.normpath(sandbox["engine_host"].removeprefix("unix://"))
            != sandbox["engine_host"].removeprefix("unix://")
            or not isinstance(sandbox.get("engine_sha256"), str)
            or _SHA256_RE.fullmatch(sandbox["engine_sha256"]) is None
            or not isinstance(sandbox.get("container_name"), str)
            or _CONTAINER_NAME_RE.fullmatch(sandbox["container_name"]) is None
            or not isinstance(sandbox.get("token"), str)
            or _SANDBOX_TOKEN_RE.fullmatch(sandbox["token"]) is None
            or not isinstance(sandbox.get("spec_sha256"), str)
            or _SHA256_RE.fullmatch(sandbox["spec_sha256"]) is None
            or sandbox.get("network_mode") not in {"none", "bridge"}
            or (sandbox.get("network_mode") == "bridge"
                and sandbox.get("network_development_only") is not True)
            or (sandbox.get("network_mode") == "none" and has_network_development)
            or sandbox.get("rootfs_readonly") is not True
            or sandbox.get("no_new_privileges") is not True
            or sandbox.get("cap_drop_all") is not True
            or sandbox.get("pid_namespace") is not True
            or sandbox.get("resource_mode") not in _SANDBOX_RESOURCE_MODES
            or (has_local and (
                not isinstance(sandbox.get("local_environment_identity_sha256"), str)
                or _SHA256_RE.fullmatch(
                    sandbox["local_environment_identity_sha256"]) is None
                or sandbox.get("local_environment_development_only") is not True))
            or (terminal and sandbox.get("container_drained") is not True)):
        raise ValueError("sandbox receipt authority 非法")


def _validate_receipt(receipt: Mapping[str, Any], path: Path) -> None:
    operation_id = receipt.get("operation_id")
    context = receipt.get("context")
    timeout_value = receipt.get("timeout_s")
    lifecycle_bound = receipt.get("kind") == "codex-resident-stage"
    current_dir = os.stat(Path(path).parent, follow_symlinks=False)
    if (not isinstance(receipt.get("version"), int)
            or isinstance(receipt.get("version"), bool)
            or receipt["version"] != _RECEIPT_VERSION
            or not isinstance(operation_id, str)
            or not _OPERATION_RE.match(operation_id)
            or Path(path).name != f"execution-{operation_id}.json"
            or not isinstance(receipt.get("owner_id"), str)
            or not 0 < len(receipt["owner_id"]) <= 128
            or not isinstance(receipt.get("kind"), str)
            or not _KIND_RE.match(receipt["kind"])
            or receipt.get("backend") != "linux-subreaper-session-v1"
            or receipt.get("containment") not in {
                "trusted-descendant-tree", _SANDBOX_BACKEND}
            or not _SHA256_RE.match(str(receipt.get("spec_sha256", "")))
            # Only the resident stage-main Codex may be bound to its owner's
            # lifecycle instead of a wall-clock deadline.  Every experiment
            # command and every other external process retains a finite
            # watchdog.  Encoding the mode in ``kind`` keeps a corrupted null
            # timeout from silently widening an ordinary receipt.
            or ((timeout_value is None) != lifecycle_bound)
            or (timeout_value is not None
                and not _finite_number(timeout_value, positive=True))
            or not _finite_number(receipt.get("term_grace_s"), positive=True)
            or not _finite_number(receipt.get("prepared_at_unix"), positive=True)
            or not isinstance(receipt.get("receipt_dir_dev"), int)
            or isinstance(receipt.get("receipt_dir_dev"), bool)
            or receipt["receipt_dir_dev"] < 0
            or not isinstance(receipt.get("receipt_dir_ino"), int)
            or isinstance(receipt.get("receipt_dir_ino"), bool)
            or receipt["receipt_dir_ino"] <= 0
            or not stat.S_ISDIR(current_dir.st_mode)
            or (receipt["receipt_dir_dev"], receipt["receipt_dir_ino"])
            != (current_dir.st_dev, current_dir.st_ino)
            or not isinstance(receipt.get("fenced_by_instance_lease"), bool)
            or not isinstance(context, dict)):
        raise ValueError("execution receipt common fields 非法")
    for key, value in context.items():
        if (not isinstance(key, str) or not _KIND_RE.match(key)
                or isinstance(value, bool)
                or not (value is None or isinstance(value, (str, int)))
                or (isinstance(value, str)
                    and (not value or len(value) > 128
                         or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)))):
            raise ValueError("execution receipt context 非法")
    capture_refs = (receipt.get("capture_stdout_ref"), receipt.get("capture_stderr_ref"))
    if any(value is not None for value in capture_refs):
        if not all(isinstance(value, str) and value for value in capture_refs):
            raise ValueError("execution capture refs 须成对")
        if (capture_refs[0] != str(_capture_path(path, operation_id, "stdout"))
                or capture_refs[1] != str(_capture_path(path, operation_id, "stderr"))):
            raise ValueError("execution capture ref 未按 operation_id 确定性命名")
    state = receipt.get("state")
    if state == "prepared":
        _validate_sandbox_receipt(
            receipt.get("sandbox"), containment=receipt.get("containment"), terminal=False)
        if receipt.get("outcome") is not None or receipt.get("capture_error") is not None:
            raise ValueError("prepared receipt 不得含 outcome")
        return
    if state == "running":
        _validate_sandbox_receipt(
            receipt.get("sandbox"), containment=receipt.get("containment"), terminal=False)
        if receipt.get("capture_error") is not None:
            raise ValueError("running receipt 不得含 capture_error")
        for key in ("helper_pid", "payload_pid", "initial_pgid"):
            if (not isinstance(receipt.get(key), int)
                    or isinstance(receipt[key], bool) or receipt[key] <= 0):
                raise ValueError("running receipt pid/pgid 非法")
        for key in ("helper_start_ticks", "payload_start_ticks"):
            value = receipt.get(key)
            if not isinstance(value, str) or not value.isascii() or not value.isdigit():
                raise ValueError("running receipt start_ticks 非法")
        deadline_value = receipt.get("deadline_at_unix")
        if (not _finite_number(receipt.get("started_at_unix"), positive=True)
                or ((deadline_value is None) != lifecycle_bound)
                or (deadline_value is not None
                    and not _finite_number(deadline_value, positive=True))):
            raise ValueError("running receipt deadline 非法")
        heartbeat_ref = receipt.get("heartbeat_ref")
        if (not isinstance(heartbeat_ref, str)
                or heartbeat_ref != str(Path(path).with_name(f"heartbeat-{operation_id}.json"))
                or not isinstance(receipt.get("guardian_heartbeat_seq"), int)
                or isinstance(receipt.get("guardian_heartbeat_seq"), bool)
                or receipt["guardian_heartbeat_seq"] < 0
                or not _finite_number(receipt.get("guardian_heartbeat_at_unix"), positive=True)
                or not _finite_number(receipt.get("last_activity_at_unix"), positive=True)
                or not isinstance(receipt.get("activity_cpu_ticks"), int)
                or receipt["activity_cpu_ticks"] < 0
                or not isinstance(receipt.get("activity_output_bytes"), int)
                or receipt["activity_output_bytes"] < 0
                or not isinstance(receipt.get("activity_descendant_count"), int)
                or receipt["activity_descendant_count"] < 0):
            raise ValueError("running receipt heartbeat/activity 非法")
        return
    if state != "terminal":
        raise ValueError("execution receipt state 非法")
    _validate_sandbox_receipt(
        receipt.get("sandbox"), containment=receipt.get("containment"), terminal=True)
    outcome = receipt.get("outcome")
    if (outcome not in _TERMINAL_OUTCOMES
            or (lifecycle_bound and outcome == "timeout")
            or receipt.get("group_drained") is not True
            or not isinstance(receipt.get("term_sent"), bool)
            or not isinstance(receipt.get("kill_sent"), bool)
            or not _finite_number(receipt.get("finished_at_unix"), positive=True)
            or (receipt.get("returncode") is not None
                and (not isinstance(receipt["returncode"], int)
                     or isinstance(receipt["returncode"], bool)))
            or (outcome == "exit" and not isinstance(receipt.get("returncode"), int))):
        raise ValueError("terminal execution receipt 非法")
    capture_error = receipt.get("capture_error")
    if capture_error is not None:
        if (capture_refs[0] is None or not isinstance(capture_error, str)
                or not capture_error or len(capture_error) > 500):
            raise ValueError("terminal capture_error 非法")
    elif capture_refs[0] is not None:
        for stream in ("stdout", "stderr"):
            fd, _identity = _verified_capture_fd(receipt, stream=stream)
            os.close(fd)


def validate_execution_receipt(receipt: Mapping[str, Any], path: Path) -> None:
    """Public strict validator for components that consume guardian authority."""
    _validate_receipt(receipt, Path(path))


def _boot_id() -> Optional[str]:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii").strip().lower()
    except (OSError, UnicodeError):
        return None


def _proc_info(pid: int) -> Optional[Tuple[str, int, int, int, str]]:
    """Return (start_ticks, ppid, pgrp, session, state) without trusting comm."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = raw.rfind(")")
        rest = raw[closing + 2:].split()
        return rest[19], int(rest[1]), int(rest[2]), int(rest[3]), rest[0]
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, UnicodeError, ValueError, IndexError) as error:
        raise ExecutionSupervisorError(f"/proc/{pid}/stat 不可靠读取") from error


def _proc_cpu_ticks(pid: int) -> int:
    """Best-effort utime+stime for activity sampling; vanished processes contribute zero."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = raw.rfind(")")
        rest = raw[closing + 2:].split()
        return int(rest[11]) + int(rest[12])
    except (FileNotFoundError, ProcessLookupError):
        return 0
    except (OSError, UnicodeError, ValueError, IndexError) as error:
        raise ExecutionSupervisorError(f"/proc/{pid}/stat CPU 采样失败") from error


def _execution_activity_sample(proc: subprocess.Popen) -> Dict[str, int]:
    descendants = _descendants(os.getpid())
    pids = {proc.pid, *descendants.keys()}
    cpu_ticks = sum(_proc_cpu_ticks(pid) for pid in pids)
    output_bytes = 0
    seen = set()
    for fd in (1, 2):
        try:
            info = os.fstat(fd)
        except OSError:
            continue
        identity = (info.st_dev, info.st_ino)
        if identity in seen or not stat.S_ISREG(info.st_mode):
            continue
        seen.add(identity)
        output_bytes += max(0, int(info.st_size))
    return {
        "activity_cpu_ticks": max(0, cpu_ticks),
        "activity_output_bytes": output_bytes,
        "activity_descendant_count": len(descendants),
    }


def _children(pid: int) -> List[int]:
    try:
        task_dir = Path(f"/proc/{pid}/task")
        tids = list(task_dir.iterdir())
    except (FileNotFoundError, ProcessLookupError):
        return []
    except OSError as error:
        raise ExecutionSupervisorError(f"/proc/{pid}/task 不可靠读取") from error
    result = set()
    for task in tids:
        try:
            raw = (task / "children").read_text(encoding="ascii").strip()
        except FileNotFoundError:
            continue  # thread exited between task enumeration and read
        except (OSError, UnicodeError) as error:
            raise ExecutionSupervisorError(
                f"/proc/{pid}/task/{task.name}/children 不可靠读取") from error
        for token in raw.split():
            try:
                child = int(token)
            except ValueError as error:
                raise ExecutionSupervisorError("/proc children 含非数字 PID") from error
            if child > 0:
                result.add(child)
    return sorted(result)


def _descendants(root_pid: int) -> Dict[int, Tuple[str, int, int, int, str]]:
    result: Dict[int, Tuple[str, int, int, int, str]] = {}
    pending = list(_children(root_pid))
    while pending:
        pid = pending.pop()
        if pid in result:
            continue
        info = _proc_info(pid)
        if info is None:
            continue
        result[pid] = info
        pending.extend(_children(pid))
    return result


def _same_process(pid: int, start_ticks: str) -> bool:
    info = _proc_info(pid)
    return info is not None and info[0] == start_ticks


def _set_subreaper() -> None:
    # PR_SET_CHILD_SUBREAPER = 36.  Do this in the single-threaded external
    # guardian, never through preexec_fn in the multithreaded orchestrator.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


class ExecutionSupervisor:
    """Shared parent-side authority for Codex and manifest executions."""

    def __init__(self, *, receipt_dir: Path, owner_id: str,
                 owner_guard: Optional[Callable[[], None]] = None,
                 fence_context_factory: Optional[Callable[[], Any]] = None,
                 term_grace_s: float = 1.0):
        if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 128:
            raise ValueError("execution supervisor owner_id 非法")
        if os.getpid() == 1:
            raise ExecutionSupervisorError(
                "orchestrator 不得作为 PID namespace PID 1 启动 guardian；"
                "PID1 退出会销毁命名空间，无法完成 owner-death receipt/fence")
        if (isinstance(term_grace_s, bool) or not isinstance(term_grace_s, (int, float))
                or not math.isfinite(float(term_grace_s))
                or not 0.05 <= float(term_grace_s) <= 30.0):
            raise ValueError("term_grace_s 须在 [0.05,30] 内")
        self.receipt_dir = Path(os.path.abspath(os.fspath(receipt_dir)))
        self.owner_id = owner_id
        self.owner_guard = owner_guard or (lambda: None)
        self.fence_context_factory = fence_context_factory
        self.term_grace_s = float(term_grace_s)
        self._guard = threading.RLock()
        self._condition = threading.Condition(self._guard)
        self._active: Dict[str, _ActiveExecution] = {}
        self._shutdown = False
        self._unsafe_error: Optional[BaseException] = None
        self._recovered = False
        self.receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        receipt_dir_fd = os.open(
            self.receipt_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(receipt_dir_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise PermissionError("execution receipt 目录身份非法")
            os.fchmod(receipt_dir_fd, 0o700)
        finally:
            os.close(receipt_dir_fd)

    @classmethod
    def standalone(cls, receipt_dir: Path) -> "ExecutionSupervisor":
        return cls(
            receipt_dir=receipt_dir,
            owner_id=f"standalone-{os.getpid()}-{secrets.token_hex(8)}")

    @staticmethod
    def _same_callable(left: Callable[..., Any], right: Callable[..., Any]) -> bool:
        """Compare a callable capability without relying on user ``__eq__``."""
        if left is right:
            return True
        left_self, right_self = getattr(left, "__self__", None), getattr(right, "__self__", None)
        left_func, right_func = getattr(left, "__func__", None), getattr(right, "__func__", None)
        return (left_self is not None and left_self is right_self
                and left_func is not None and left_func is right_func)

    def binds_fenced_owner(self, owner_guard: Callable[[], None]) -> bool:
        """Whether this authority is mechanically fenced by ``owner_guard``'s owner."""
        if (self.fence_context_factory is None
                or not self._same_callable(self.owner_guard, owner_guard)):
            return False
        guard_owner = getattr(owner_guard, "__self__", None)
        fence_owner = getattr(self.fence_context_factory, "__self__", None)
        if guard_owner is None or fence_owner is not guard_owner:
            return False
        expected_owner_id = getattr(guard_owner, "owner_id", None)
        return (isinstance(expected_owner_id, str)
                and self.owner_id == expected_owner_id)

    def recover_previous_generation(self) -> None:
        """Resolve pre-spawn intents; reject unprovable prior running trees."""
        with self._guard:
            if self._recovered:
                return
            self.owner_guard()
            for path in sorted(self.receipt_dir.glob("execution-*.json")):
                try:
                    receipt = read_receipt(path)
                    _validate_receipt(receipt, path)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    raise ExecutionRecoveryError(
                        f"execution receipt 损坏，拒绝新执行: {path.name}") from error
                state = receipt.get("state")
                if state == "terminal":
                    if (receipt.get("outcome") not in _TERMINAL_OUTCOMES
                            or receipt.get("group_drained") is not True):
                        raise ExecutionRecoveryError(
                            f"execution terminal receipt 不完整: {path.name}")
                    continue
                if state == "prepared":
                    if (self.fence_context_factory is None
                            or receipt.get("fenced_by_instance_lease") is not True):
                        raise ExecutionRecoveryError(
                            "旧 prepared receipt 无 instance fence，不能证明 guardian 已消失；"
                            f"拒绝启动: {path.name}")
                    terminal = _terminal_from(
                        receipt, outcome="owner_lost_before_start",
                        returncode=None, started_at_unix=None,
                        term_sent=False, kill_sent=False,
                        recovered_by_owner_id=self.owner_id)
                    if receipt.get("capture_stdout_ref") is not None:
                        try:
                            for stream in ("stdout", "stderr"):
                                identity = _capture_identity_from_path(
                                    Path(receipt[f"capture_{stream}_ref"]))
                                for field, value in identity.items():
                                    terminal[f"capture_{stream}_{field}"] = value
                        except Exception as error:
                            for stream in ("stdout", "stderr"):
                                for field in ("sha256", "bytes", "device", "inode"):
                                    terminal.pop(f"capture_{stream}_{field}", None)
                            terminal["capture_error"] = f"{type(error).__name__}: {error}"[:500]
                    atomic_write_receipt(path, terminal)
                    continue
                if state == "running":
                    raise ExecutionRecoveryError(
                        "发现无 guardian fence 的 prior running receipt；"
                        f"无法证明旧子树已空，拒绝启动: {path.name}")
                raise ExecutionRecoveryError(
                    f"execution receipt state 非法: {path.name}")
            self._recovered = True

    def _prepared_receipt(self, *, operation_id: str, kind: str,
                          spec_sha256: str, timeout_s: Optional[float],
                          operation_context: Mapping[str, Any],
                          external_container: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        root_info = os.stat(self.receipt_dir)
        prepared = {
            "version": _RECEIPT_VERSION,
            "operation_id": operation_id,
            "kind": kind,
            "owner_id": self.owner_id,
            "state": "prepared",
            "outcome": None,
            "backend": "linux-subreaper-session-v1",
            "containment": (_SANDBOX_BACKEND if external_container is not None
                            else "trusted-descendant-tree"),
            "boot_id": _boot_id(),
            "receipt_dir_dev": root_info.st_dev,
            "receipt_dir_ino": root_info.st_ino,
            "spec_sha256": spec_sha256,
            "timeout_s": timeout_s,
            "term_grace_s": self.term_grace_s,
            "context": dict(operation_context),
            "prepared_at_unix": time.time(),
            "fenced_by_instance_lease": self.fence_context_factory is not None,
        }
        if external_container is not None:
            prepared["sandbox"] = dict(external_container)
        return prepared

    def _poison(self, error: BaseException) -> None:
        """Irreversibly stop this authority after an unprovable guardian state."""
        with self._guard:
            if self._unsafe_error is None:
                self._unsafe_error = error
            self._shutdown = True
            for active in list(self._active.values()):
                active.cancel()

    def run(self, cmd: Sequence[str], *, stdin=None, capture_output: bool = False,
            stdout=None, stderr=None, timeout_s: Optional[float],
            cwd: Optional[Path] = None,
            env: Optional[Mapping[str, str]] = None,
            pass_fds: Sequence[int] = (), kind: str = "external",
            operation_context: Optional[Mapping[str, Any]] = None,
            external_container: Optional[Mapping[str, Any]] = None,
            progress_observer: Optional[Callable[[], bool]] = None,
            progress_interval_s: float = 5.0) -> ExecutionResult:
        """Execute one command and return only after its descendant tree is empty.

        ``progress_observer`` is a trusted parent-side, read-only observation
        hook.  It never receives argv, process handles or lifecycle authority;
        returning ``True`` asks the already-registered guardian to cancel this
        exact execution.  The hook may itself perform a bounded Codex operator
        turn while the guardian continues supervising the workload.
        """
        if not isinstance(cmd, (list, tuple)) or not cmd:
            raise ValueError("cmd 须为非空 argv sequence")
        argv = []
        for value in cmd:
            if not isinstance(value, str) or not value or "\x00" in value:
                raise ValueError("cmd argv 须为无 NUL 非空字符串")
            argv.append(value)
        if not _KIND_RE.match(kind):
            raise ValueError("execution kind 非法")
        receipt_context: Dict[str, Any] = {}
        for key, value in (operation_context or {}).items():
            if (not isinstance(key, str) or not _KIND_RE.match(key)
                    or isinstance(value, bool)
                    or not (value is None or isinstance(value, (str, int)))):
                raise ValueError("operation_context 只接受有界标量")
            if (isinstance(value, str)
                    and (not value or len(value) > 128
                         or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value))):
                raise ValueError("operation_context 字符串非法")
            receipt_context[key] = value
        lifecycle_bound = timeout_s is None
        if lifecycle_bound:
            if kind != "codex-resident-stage":
                raise ValueError(
                    "仅 codex-resident-stage 允许 timeout_s=None 的 owner 生命周期")
        elif kind == "codex-resident-stage":
            raise ValueError(
                "codex-resident-stage 必须使用 timeout_s=None 的 owner 生命周期")
        elif (isinstance(timeout_s, bool)
              or not isinstance(timeout_s, (int, float))
              or not math.isfinite(float(timeout_s)) or float(timeout_s) <= 0):
            raise ValueError("timeout_s 须为正有限数或 resident stage 的 None")
        else:
            timeout_s = float(timeout_s)
        if progress_observer is not None and not callable(progress_observer):
            raise ValueError("progress_observer 须为 callable 或 None")
        if (isinstance(progress_interval_s, bool)
                or not isinstance(progress_interval_s, (int, float))
                or not math.isfinite(float(progress_interval_s))
                or not 0.05 <= float(progress_interval_s) <= 3600.0):
            raise ValueError("progress_interval_s 须在 [0.05,3600] 内")
        progress_interval_s = float(progress_interval_s)
        if capture_output and (stdout is not None or stderr is not None):
            raise ValueError("capture_output 不得与 stdout/stderr 同时使用")
        if stdin == subprocess.PIPE:
            raise ValueError("supervisor 不接受 stdin=PIPE；请传已打开文件")
        target_fds = tuple(dict.fromkeys(int(fd) for fd in pass_fds))
        if any(fd < 3 for fd in target_fds):
            raise ValueError("pass_fds 只接受 >=3 的 descriptor")
        for fd in target_fds:
            os.fstat(fd)
        target_cwd = None if cwd is None else os.path.abspath(os.fspath(cwd))
        target_env: Dict[str, str] = {}
        if env is None:
            target_env = dict(os.environ)
        else:
            for key, value in env.items():
                if (not isinstance(key, str) or not isinstance(value, str)
                        or "\x00" in key or "\x00" in value or "=" in key):
                    raise ValueError("env 须为无 NUL 字符串映射")
                target_env[key] = value
        sandbox = _normalize_external_container(external_container)
        spec_for_hash = {
            "argv": argv, "cwd": target_cwd, "timeout_s": timeout_s,
            "env": sorted(target_env.items()),
            "pass_fd_count": len(target_fds), "capture_output": capture_output,
            "kind": kind,
            "context": receipt_context,
            "external_container": sandbox,
        }
        # Preserve the historical identity for every existing caller.  The
        # observation cadence participates only when that optional control is
        # actually enabled.
        if progress_observer is not None:
            spec_for_hash.update({
                "progress_observer": True,
                "progress_interval_s": progress_interval_s,
            })
        spec_sha256 = "sha256:" + hashlib.sha256(
            _canonical_json(spec_for_hash)).hexdigest()
        operation_id = f"exec-{secrets.token_hex(16)}"
        receipt_path = self.receipt_dir / f"execution-{operation_id}.json"
        prepared = self._prepared_receipt(
            operation_id=operation_id, kind=kind,
            spec_sha256=spec_sha256, timeout_s=timeout_s,
            operation_context=receipt_context,
            external_container=sandbox)
        capture_stdout_path = _capture_path(receipt_path, operation_id, "stdout")
        capture_stderr_path = _capture_path(receipt_path, operation_id, "stderr")
        if capture_output:
            prepared.update({
                "capture_stdout_ref": str(capture_stdout_path),
                "capture_stderr_ref": str(capture_stderr_path),
            })

        old_sigint_handler = None
        manage_sigint = threading.current_thread() is threading.main_thread()
        if manage_sigint:
            old_sigint_handler = signal.getsignal(signal.SIGINT)
            if (old_sigint_handler != signal.SIG_DFL
                    and old_sigint_handler != signal.SIG_IGN
                    and not callable(old_sigint_handler)):
                raise ExecutionSupervisorError(
                    f"不支持的 SIGINT handler: {old_sigint_handler!r}")

        out_tmp = err_tmp = None
        if capture_output:
            try:
                out_tmp = _open_capture_file(capture_stdout_path)
                err_tmp = _open_capture_file(capture_stderr_path)
            except BaseException:
                for stream in (out_tmp, err_tmp):
                    if stream is not None:
                        try:
                            stream.close()
                        except BaseException:
                            pass
                for capture_path in (capture_stdout_path, capture_stderr_path):
                    try:
                        capture_path.unlink()
                    except OSError:
                        pass
                raise
        helper_stdout = out_tmp if capture_output else stdout
        helper_stderr = err_tmp if capture_output else stderr
        spec_file = tempfile.TemporaryFile()
        owner_read = owner_write = -1
        helper: Optional[subprocess.Popen] = None
        active: Optional[_ActiveExecution] = None
        spawn_error: Optional[BaseException] = None
        spawn_attempted = False
        deferred_sigint_errors: List[BaseException] = []
        deferred_default_sigint = False
        wait_notes: List[BaseException] = []
        sigint_handler_installed = False
        effective_sigint_handler = old_sigint_handler
        spec_file_closed = False
        pre_wait_error: Optional[BaseException] = None

        def close_spec_once() -> Optional[BaseException]:
            nonlocal spec_file_closed
            if spec_file_closed:
                return None
            # Linux close(EINTR) must not be retried: the numeric descriptor may
            # already have been reused even though close reported an error.
            spec_file_closed = True
            try:
                spec_file.close()
            except BaseException as error:
                return error
            return None

        def defer_sigint(signum, frame) -> None:  # noqa: ANN001
            nonlocal deferred_default_sigint, effective_sigint_handler
            # Preserve the embedding's existing SIGINT semantics without
            # allowing a Python exception to escape across the registered
            # guardian lifecycle.  A benign custom handler still runs at once;
            # only a handler that would raise causes cancellation and a
            # deferred re-raise after terminal validation.
            handler = effective_sigint_handler
            if handler == signal.SIG_IGN:
                return
            if handler == signal.SIG_DFL:
                deferred_default_sigint = True
            elif callable(handler):
                error_count = len(deferred_sigint_errors)
                try:
                    handler(signum, frame)
                except BaseException as error:
                    deferred_sigint_errors.append(error)
                # A custom handler may intentionally replace its disposition.
                # Keep our defer barrier installed until terminal validation,
                # but restore the handler's chosen successor afterwards.
                try:
                    installed = signal.getsignal(signal.SIGINT)
                    if installed is not defer_sigint:
                        effective_sigint_handler = installed
                        signal.signal(signal.SIGINT, defer_sigint)
                except BaseException as error:
                    deferred_sigint_errors.append(error)
                if len(deferred_sigint_errors) == error_count:
                    return
            else:  # validated before any resources or guardian are created
                deferred_sigint_errors.append(
                    ExecutionSupervisorError(
                        f"不支持的 SIGINT handler: {handler!r}"))
            # Publish rejection before cancellation so no concurrent caller
            # can pass the spawn gate while this execution is converging.
            self._shutdown = True
            if active is not None:
                try:
                    active.cancel()
                except BaseException as error:
                    wait_notes.append(error)

        try:
            spec = {
                "version": 1,
                "argv": argv,
                "cwd": target_cwd,
                "target_pass_fds": list(target_fds),
                "env": target_env,
                "timeout_s": timeout_s,
                "deadline_monotonic_s": (
                    None if timeout_s is None else time.monotonic() + timeout_s),
                "term_grace_s": self.term_grace_s,
                "receipt_path": str(receipt_path),
                "prepared": prepared,
                "external_container": sandbox,
            }
            spec_payload = _canonical_json(spec)
            if len(spec_payload) > _MAX_SPEC_BYTES:
                raise ValueError("execution spec 超过大小上限")
            spec_file.write(spec_payload)
            spec_file.flush()
            spec_file.seek(0)
            fence_cm = (self.fence_context_factory()
                        if self.fence_context_factory is not None
                        else contextlib.nullcontext(-1))

            with self._guard, _GLOBAL_CONDITION:
                self.recover_previous_generation()
                self.owner_guard()
                if self._unsafe_error is not None:
                    raise ExecutionSupervisorError(
                        "execution supervisor 已因不可证明的 guardian 状态永久停机") \
                        from self._unsafe_error
                if self._shutdown or _GLOBAL_HARD_STOP:
                    raise ExecutionSupervisorError(
                        "execution supervisor 已进入 hard-stop，拒绝启动新调用")
                atomic_write_receipt(receipt_path, prepared)
                try:
                    with fence_cm as fence_fd:
                        pipe_flags = (getattr(os, "O_CLOEXEC", 0)
                                      | getattr(os, "O_NONBLOCK", 0))
                        owner_read, owner_write = os.pipe2(pipe_flags)
                        inherited = [spec_file.fileno(), owner_read, *target_fds]
                        helper_args = [
                            sys.executable, "-I", os.path.abspath(__file__), _HELPER_FLAG,
                            "--spec-fd", str(spec_file.fileno()),
                            "--owner-fd", str(owner_read),
                            "--owner-pid", str(os.getpid()),
                            "--fence-fd", str(fence_fd),
                        ]
                        if fence_fd >= 0:
                            inherited.append(fence_fd)
                        if manage_sigint and old_sigint_handler != signal.SIG_IGN:
                            signal.signal(signal.SIGINT, defer_sigint)
                            sigint_handler_installed = True
                        # Neither OSError nor another BaseException proves that
                        # Popen failed before fork/exec: an injected wrapper or
                        # async interruption may raise after a real child was
                        # created.  From this point every exception is therefore
                        # ambiguous and permanently poisons the authority.
                        spawn_attempted = True
                        helper = subprocess.Popen(
                            helper_args, stdin=stdin,
                            stdout=helper_stdout, stderr=helper_stderr,
                            env={"PATH": os.environ.get("PATH", os.defpath),
                                 "LANG": os.environ.get("LANG", "C.UTF-8")},
                            close_fds=True,
                            pass_fds=tuple(dict.fromkeys(inherited)),
                            start_new_session=True)
                        active = _ActiveExecution(operation_id, helper, owner_write)
                        self._active[operation_id] = active
                        _GLOBAL_ACTIVE[operation_id] = active
                        if deferred_sigint_errors or deferred_default_sigint:
                            active.cancel()
                        owner_write = -1
                        os.close(owner_read)
                        owner_read = -1
                        _GLOBAL_CONDITION.notify_all()
                except BaseException as error:
                    spawn_error = error
                    ambiguous = ExecutionSupervisorError(
                        "guardian fence/spawn 结果不可证明；"
                        "supervisor 已永久停机")
                    if self._unsafe_error is None:
                        self._unsafe_error = ambiguous
                    self._shutdown = True
                    for registered in list(self._active.values()):
                        registered.cancel()
                    raise
        except BaseException as primary:
            spec_close_error = close_spec_once()
            if spec_close_error is not None:
                note = getattr(primary, "add_note", None)
                if callable(note):
                    note("execution spec close 同时失败: "
                         f"{type(spec_close_error).__name__}: {spec_close_error}")
            # Closing the sole owner write end is enough to stop an ambiguously
            # spawned guardian even if an async exception landed before the
            # Popen result could be registered.
            for fd in (owner_write, owner_read):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if active is not None:
                active.cancel()
                active.close_owner_pipe()
            if helper is not None:
                while True:
                    try:
                        helper.wait()
                        break
                    except BaseException as secondary:
                        note = getattr(primary, "add_note", None)
                        if callable(note):
                            note(f"guardian reap 期间又受到 {type(secondary).__name__}: {secondary}")
                        continue
                if active is not None:
                    with self._guard, _GLOBAL_CONDITION:
                        self._active.pop(operation_id, None)
                        _GLOBAL_ACTIVE.pop(operation_id, None)
                        self._condition.notify_all()
                        _GLOBAL_CONDITION.notify_all()
            elif not spawn_attempted and isinstance(spawn_error, OSError):
                terminal = dict(prepared)
                if prepared.get("sandbox") is not None:
                    sandbox_terminal = dict(prepared["sandbox"])
                    # No guardian Popen was attempted, so no payload launcher
                    # could have contacted the container daemon.
                    sandbox_terminal["container_drained"] = True
                    terminal["sandbox"] = sandbox_terminal
                terminal.update({
                    "state": "terminal", "outcome": "spawn_failed",
                    "returncode": None, "started_at_unix": None,
                    "finished_at_unix": time.time(), "group_drained": True,
                    "term_sent": False, "kill_sent": False,
                    "spawn_error": type(spawn_error).__name__,
                })
                if capture_output:
                    assert out_tmp is not None and err_tmp is not None
                    try:
                        for stream, capture_path, capture_file in (
                                ("stdout", capture_stdout_path, out_tmp),
                                ("stderr", capture_stderr_path, err_tmp)):
                            identity = _capture_identity(capture_path, capture_file.fileno())
                            for field, value in identity.items():
                                terminal[f"capture_{stream}_{field}"] = value
                    except Exception as capture_error:
                        for stream in ("stdout", "stderr"):
                            for field in ("sha256", "bytes", "device", "inode"):
                                terminal.pop(f"capture_{stream}_{field}", None)
                        terminal["capture_error"] = (
                            f"{type(capture_error).__name__}: {capture_error}"[:500])
                try:
                    atomic_write_receipt(receipt_path, terminal)
                except BaseException as receipt_error:
                    note = getattr(primary, "add_note", None)
                    if callable(note):
                        note(f"spawn_failed receipt 写入失败: {receipt_error}")
            if spawn_error is not None:
                ambiguous = self._unsafe_error or ExecutionSupervisorError(
                    "guardian fence/spawn 失败")
                note = getattr(primary, "add_note", None)
                if callable(note):
                    note(str(ambiguous))
            for stream in (out_tmp, err_tmp):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException:
                        pass
            sigint_restored = not sigint_handler_installed
            if sigint_handler_installed:
                try:
                    signal.signal(signal.SIGINT, effective_sigint_handler)
                    sigint_restored = True
                except BaseException as error:
                    note = getattr(primary, "add_note", None)
                    if callable(note):
                        note(f"SIGINT handler 恢复失败: {error}")
            if deferred_sigint_errors:
                signal_error = deferred_sigint_errors[0]
                note = getattr(signal_error, "add_note", None)
                if callable(note):
                    note("guardian spawn/cleanup 同时失败: "
                         f"{type(primary).__name__}: {primary}")
                    for secondary in deferred_sigint_errors[1:]:
                        note("cleanup 期间又受到 "
                             f"{type(secondary).__name__}: {secondary}")
                raise signal_error from primary
            if deferred_default_sigint and sigint_restored:
                os.kill(os.getpid(), signal.SIGINT)
            raise

        pre_wait_error = close_spec_once()
        if pre_wait_error is not None and active is not None:
            self._shutdown = True
            try:
                active.cancel()
            except BaseException as secondary:
                wait_notes.append(secondary)

        primary_wait_error: Optional[BaseException] = pre_wait_error
        observer_cancelled = False
        while True:
            try:
                if primary_wait_error is not None:
                    # A wait/cancel interruption permanently rejects new
                    # spawn for this supervisor, then keeps converging.
                    self._shutdown = True
                    active.cancel()
                    helper.wait()
                    break
                if progress_observer is None or observer_cancelled:
                    helper.wait()
                    break
                try:
                    helper.wait(timeout=progress_interval_s)
                    break
                except subprocess.TimeoutExpired:
                    # Timeout here is only the observation cadence.  The
                    # guardian independently enforces the execution deadline.
                    request_cancel = progress_observer()
                    if request_cancel is not False and request_cancel is not True:
                        raise ValueError(
                            "progress_observer 返回值须为 bool")
                    if request_cancel:
                        active.cancel()
                        observer_cancelled = True
            except BaseException as error:
                self._shutdown = True
                if primary_wait_error is None:
                    primary_wait_error = error
                else:
                    wait_notes.append(error)
                continue

        receipt: Optional[Dict[str, Any]] = None
        protocol_error: Optional[BaseException] = None
        # Spawn and terminal validation use the same lock.  Thus an abnormal
        # guardian can poison the authority before any concurrent caller starts
        # another tree in the few bytecodes after wait().
        with self._guard, _GLOBAL_CONDITION:
            try:
                if helper.returncode != 0:
                    raise ExecutionSupervisorError(
                        f"guardian 异常退出 (exit={helper.returncode})")
                receipt = read_receipt(receipt_path)
                _validate_receipt(receipt, receipt_path)
                if (receipt.get("operation_id") != operation_id
                        or receipt.get("owner_id") != self.owner_id
                        or receipt.get("state") != "terminal"
                        or receipt.get("group_drained") is not True):
                    raise ExecutionSupervisorError("guardian terminal receipt 身份/收口非法")
            except BaseException as error:
                protocol_error = error
                if self._unsafe_error is None:
                    self._unsafe_error = error
                self._shutdown = True
                for other_id, other in list(self._active.items()):
                    if other_id != operation_id:
                        other.cancel()
            finally:
                self._active.pop(operation_id, None)
                _GLOBAL_ACTIVE.pop(operation_id, None)
                active.close_owner_pipe()
                self._condition.notify_all()
                _GLOBAL_CONDITION.notify_all()

        sigint_restored = not sigint_handler_installed
        if sigint_handler_installed:
            try:
                signal.signal(signal.SIGINT, effective_sigint_handler)
                sigint_restored = True
            except BaseException as error:
                self._shutdown = True
                if primary_wait_error is None:
                    primary_wait_error = error
                else:
                    wait_notes.append(error)
        if deferred_sigint_errors:
            self._shutdown = True
            if primary_wait_error is None:
                primary_wait_error = deferred_sigint_errors[0]
            wait_notes.extend(deferred_sigint_errors[1:])
        if deferred_default_sigint:
            self._shutdown = True
            if sigint_restored:
                # Re-enter the kernel's original default disposition only
                # after the guardian has proved the tree drained and the
                # active registry no longer advertises this execution.
                os.kill(os.getpid(), signal.SIGINT)

        stdout_bytes = stderr_bytes = None
        if out_tmp is not None:
            out_tmp.seek(0)
            stdout_bytes = out_tmp.read()
            out_tmp.close()
        if err_tmp is not None:
            err_tmp.seek(0)
            stderr_bytes = err_tmp.read()
            err_tmp.close()
        if protocol_error is not None:
            unsafe = ExecutionSupervisorError(
                "guardian 未提供可证明 drained 的 terminal；supervisor 已永久停机")
            if primary_wait_error is not None:
                note = getattr(primary_wait_error, "add_note", None)
                if callable(note):
                    note(f"{unsafe}: {type(protocol_error).__name__}: {protocol_error}")
                raise primary_wait_error from protocol_error
            raise unsafe from protocol_error
        if primary_wait_error is not None:
            try:
                primary_wait_error.execution_receipt = receipt
                primary_wait_error.execution_receipt_path = receipt_path
            except BaseException:
                pass
            note = getattr(primary_wait_error, "add_note", None)
            if callable(note):
                for secondary in wait_notes:
                    note(f"cleanup 期间又受到 {type(secondary).__name__}: {secondary}")
            raise primary_wait_error
        assert receipt is not None
        outcome = receipt.get("outcome")
        if outcome == "timeout":
            if timeout_s is None:
                raise ExecutionSupervisorError(
                    "owner-lifecycle execution 不得产生 timeout receipt")
            raise SupervisedTimeoutExpired(
                argv, timeout_s, output=stdout_bytes, stderr=stderr_bytes,
                receipt=receipt, receipt_path=receipt_path)
        if outcome == "cancelled":
            raise ExecutionCancelled(
                "execution 已被 hard-stop/cancel 终止",
                receipt=receipt, receipt_path=receipt_path)
        if outcome in {"owner_lost", "lingering_descendant", "spawn_failed"}:
            raise ExecutionCleanupError(
                f"execution 未产生可接受结果: {outcome}",
                receipt=receipt, receipt_path=receipt_path)
        if outcome != "exit" or not isinstance(receipt.get("returncode"), int):
            raise ExecutionSupervisorError(f"guardian terminal outcome 非法: {outcome!r}")
        return ExecutionResult(
            args=argv, returncode=int(receipt["returncode"]),
            stdout=stdout_bytes, stderr=stderr_bytes,
            receipt=receipt, receipt_path=receipt_path,
            heartbeat_path=Path(receipt["heartbeat_ref"])
            if receipt.get("heartbeat_ref") else None)

    def close(self, *, timeout_s: float = 10.0) -> None:
        """Reject new work, cancel active guardians and wait for exact emptiness."""
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(float(timeout_s)) or float(timeout_s) < 0):
            raise ValueError("close timeout_s 须为非负有限数")
        with self._condition:
            self._shutdown = True
            for active in list(self._active.values()):
                active.cancel()
            deadline = time.monotonic() + float(timeout_s)
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ExecutionSupervisorError(
                        f"{len(self._active)} 个 execution 未在 close deadline 内收敛")
                self._condition.wait(min(0.1, remaining))
            if self._unsafe_error is not None:
                raise ExecutionSupervisorError(
                    "execution supervisor 存在不可证明的 prior guardian；"
                    "拒绝报告 close 成功") from self._unsafe_error

    @property
    def active_count(self) -> int:
        with self._guard:
            return len(self._active)

    @property
    def closed(self) -> bool:
        with self._guard:
            return self._shutdown and not self._active and self._unsafe_error is None


# ---------------------------------------------------------------- guardian --

_GUARDIAN_EVENT: Optional[str] = None


def _guardian_signal(signum, _frame) -> None:  # noqa: ANN001
    global _GUARDIAN_EVENT
    event = "owner_lost" if signum == signal.SIGUSR1 else "cancelled"
    if _GUARDIAN_EVENT != "owner_lost":
        _GUARDIAN_EVENT = event


def _arm_parent_death_signal(expected_owner_pid: int) -> Optional[str]:
    """Arm a non-fatal owner-death signal and close the pre-arm race."""
    if not hasattr(signal, "pthread_sigmask"):
        raise OSError(errno.ENOSYS, "pthread_sigmask unavailable")
    signal.pthread_sigmask(
        signal.SIG_UNBLOCK,
        {signal.SIGUSR1, signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_PDEATHSIG = 1.  SIGUSR1 is handled; unlike SIGKILL/SIGTERM it does
    # not prevent the guardian from draining the tree and fsyncing its receipt.
    if libc.prctl(1, int(signal.SIGUSR1), 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    # The parent may have died before prctl.  In that case no signal is
    # retroactive, so PPID revalidation is the other half of the protocol.
    return "owner_lost" if os.getppid() != expected_owner_pid else None


def _read_spec_fd(fd: int) -> Dict[str, Any]:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, remaining = [], _MAX_SPEC_BYTES + 1
    while remaining:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > _MAX_SPEC_BYTES:
        raise ValueError("execution spec 超过大小上限")
    return _strict_json_bytes(raw, limit=_MAX_SPEC_BYTES)


def _owner_event(owner_fd: int, timeout_s: float) -> Optional[str]:
    if _GUARDIAN_EVENT is not None:
        return _GUARDIAN_EVENT
    readable, _, _ = select.select([owner_fd], [], [], max(0.0, timeout_s))
    if not readable:
        return None
    try:
        payload = os.read(owner_fd, 4096)
    except BlockingIOError:
        return None
    if not payload:
        return "owner_lost"
    return "cancelled" if b"C" in payload else "cancelled"


def _poll_leader(proc: subprocess.Popen) -> Optional[int]:
    return proc.poll()


def _all_children_drained(proc: subprocess.Popen) -> bool:
    """Kernel authority: ECHILD after the leader is reaped means tree empty."""
    # Never let waitpid(-1) steal the leader from Popen.  Once poll() has
    # reaped it, every remaining direct child is an adopted descendant because
    # this guardian is a subreaper.  waitpid==0 means at least one live child;
    # only ECHILD is a positive emptiness proof.
    if proc.returncode is None:
        return False
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            return False


def _signal_tree(root_pid: int, leader_pid: int, leader_start: str,
                 sig: signal.Signals, *, allow_group: bool) -> Tuple[int, int]:
    signalled = 0
    errors = 0
    try:
        descendants = _descendants(root_pid)
    except BaseException:
        descendants = {}
        errors += 1
    try:
        leader_matches = allow_group and _same_process(leader_pid, leader_start)
    except BaseException:
        leader_matches = False
        errors += 1
    if leader_matches:
        try:
            os.killpg(leader_pid, sig)
            signalled += 1
        except ProcessLookupError:
            pass
        except OSError:
            errors += 1
    for pid, info in descendants.items():
        try:
            matches = _same_process(pid, info[0])
        except BaseException:
            errors += 1
            continue
        if not matches:
            continue
        try:
            os.kill(pid, sig)
            signalled += 1
        except ProcessLookupError:
            pass
        except OSError:
            errors += 1
    return signalled, errors


def _tree_empty(root_pid: int, proc: subprocess.Popen) -> bool:
    _poll_leader(proc)
    return _all_children_drained(proc)


def _terminate_tree(proc: subprocess.Popen, *, leader_start: str,
                    grace_s: float) -> Tuple[bool, bool, int, int]:
    root_pid = os.getpid()
    signalled, signal_errors = _signal_tree(
        root_pid, proc.pid, leader_start, signal.SIGTERM, allow_group=True)
    term_sent = bool(signalled)
    term_deadline = time.monotonic() + grace_s
    max_descendants = 0
    while time.monotonic() < term_deadline:
        _poll_leader(proc)
        try:
            descendants = _descendants(root_pid)
        except BaseException:
            descendants = {}
            signal_errors += 1
        max_descendants = max(max_descendants, len(descendants))
        if _all_children_drained(proc):
            return term_sent, False, max_descendants, signal_errors
        if descendants or proc.returncode is None:
            count, errors = _signal_tree(
                root_pid, proc.pid, leader_start, signal.SIGTERM,
                allow_group=proc.returncode is None)
            term_sent = bool(count) or term_sent
            signal_errors += errors
        time.sleep(0.02)
    kill_sent = False
    while True:
        _poll_leader(proc)
        try:
            descendants = _descendants(root_pid)
        except BaseException:
            descendants = {}
            signal_errors += 1
        max_descendants = max(max_descendants, len(descendants))
        if _all_children_drained(proc):
            return term_sent, kill_sent, max_descendants, signal_errors
        count, errors = _signal_tree(
            root_pid, proc.pid, leader_start, signal.SIGKILL,
            allow_group=proc.returncode is None)
        kill_sent = bool(count) or kill_sent
        signal_errors += errors
        # Unkillable D-state/fork storms deliberately keep the guardian and
        # lease fence alive forever instead of publishing a false terminal.
        time.sleep(0.02)


def _fsync_output_fds() -> None:
    while True:
        try:
            seen = set()
            for fd in (1, 2):
                try:
                    info = os.fstat(fd)
                except OSError:
                    continue
                identity = (info.st_dev, info.st_ino)
                if identity in seen or not stat.S_ISREG(info.st_mode):
                    continue
                seen.add(identity)
                os.fsync(fd)
            return
        except BaseException:
            # Output durability is part of terminal publication.  Keep the
            # lease fence and retry rather than release on a transient storage
            # failure or falsely claim a durable terminal.
            time.sleep(1.0)


def _write_receipt_until_durable(path: Path, receipt: Mapping[str, Any]) -> None:
    while True:
        try:
            atomic_write_receipt(path, receipt)
            return
        except BaseException:
            # Releasing the delegated flock without a durable receipt would
            # let a replacement owner overlap an unaccounted execution.
            time.sleep(1.0)


def _release_fence(fence_fd: int) -> None:
    if fence_fd >= 0:
        try:
            os.close(fence_fd)
        except OSError:
            pass


def _sandbox_engine_call(
        sandbox: Mapping[str, Any], args: Sequence[str]) -> subprocess.CompletedProcess:
    """Run one bounded trusted-engine control command from the guardian."""
    if _executable_identity(sandbox["engine_path"]) != sandbox["engine_sha256"]:
        raise ExecutionSupervisorError("sandbox engine bytes 与 prepared receipt 不一致")
    return subprocess.run(
        [sandbox["engine_path"], *args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5.0,
        env={
            "PATH": os.path.dirname(sandbox["engine_path"]) or os.defpath,
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "DOCKER_HOST": sandbox["engine_host"],
        }, check=False)


def _sandbox_container_row(sandbox: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    result = _sandbox_engine_call(sandbox, [
        "container", "ls", "--all",
        "--filter", f"name=^/{sandbox['container_name']}$",
        "--format", "{{.Names}}|{{.Label \"" + _SANDBOX_LABEL + "\"}}",
    ])
    if result.returncode != 0:
        raise ExecutionSupervisorError(
            "sandbox engine 无法枚举 exact container identity")
    if len(result.stdout) > 4096 or len(result.stderr) > 4096:
        raise ExecutionSupervisorError("sandbox engine 枚举输出越界")
    try:
        lines = [line for line in result.stdout.decode("utf-8").splitlines() if line]
    except UnicodeDecodeError as error:
        raise ExecutionSupervisorError("sandbox engine 枚举输出非 UTF-8") from error
    if not lines:
        return None
    if len(lines) != 1 or "|" not in lines[0]:
        raise ExecutionSupervisorError("sandbox engine exact filter 返回非唯一容器")
    name, token = lines[0].split("|", 1)
    return name, token


def _drain_external_container(
        prepared: Mapping[str, Any], *, capture_logs: bool = False) -> Optional[Dict[str, Any]]:
    """Prove a Docker sandbox absent before publishing terminal authority.

    Docker workloads are not descendants of the CLI process.  Killing the
    local process group therefore is not an emptiness proof.  The guardian
    holds the instance fence and repeatedly asks the trusted local daemon for
    the exact random name+label identity, force-removing only a matching
    container.  Daemon loss, engine drift, or an identity collision keeps the
    fence alive just like an unkillable D-state descendant.
    """
    raw = prepared.get("sandbox")
    if raw is None:
        return None
    sandbox = dict(raw)
    while True:
        try:
            row = _sandbox_container_row(sandbox)
            if row is None:
                sandbox["container_drained"] = True
                return sandbox
            if row != (sandbox["container_name"], sandbox["token"]):
                raise ExecutionSupervisorError(
                    "sandbox container name/label identity 冲突；拒绝误杀")
            if capture_logs:
                try:
                    logs = _sandbox_engine_call(sandbox, [
                        "container", "logs", sandbox["container_name"]])
                    if (logs.returncode == 0
                            and len(logs.stdout) + len(logs.stderr) <= 4 * 1024 * 1024):
                        marker = b"\n[sandbox guardian recovered pre-cleanup logs]\n"
                        _write_all(1, marker + logs.stdout)
                        if logs.stderr:
                            _write_all(2, logs.stderr)
                except BaseException:
                    # Container absence is the authority.  Best-effort failure
                    # logs must never prevent force-removal or release a false
                    # containment receipt.
                    pass
            removed = _sandbox_engine_call(sandbox, [
                "container", "rm", "--force", "--volumes",
                sandbox["container_name"],
            ])
            if removed.returncode != 0:
                raise ExecutionSupervisorError("sandbox container force-remove 失败")
        except BaseException:
            time.sleep(1.0)
            continue
        time.sleep(0.02)


def _await_external_registration_or_runner_exit(
        prepared: Mapping[str, Any], proc: subprocess.Popen) -> None:
    """Close the daemon CREATE late-commit race before killing the trusted runner.

    If cancellation lands while ``docker create`` is in flight, killing the CLI
    and observing one absent name is not proof: the daemon could commit the
    accepted request just afterwards.  The sandbox runner is trusted host
    control code, so the guardian keeps its fence and lets registration either
    become observable (then normal termination/drain owns it) or return/exit.
    No bounded timeout is used here; uncertainty retains the fence rather than
    publishing false containment.
    """
    sandbox = prepared.get("sandbox")
    if not isinstance(sandbox, dict):
        return
    expected = (sandbox["container_name"], sandbox["token"])
    while _poll_leader(proc) is None:
        try:
            row = _sandbox_container_row(sandbox)
            if row == expected:
                return
            if row is not None:
                # A name collision with a foreign label may never be killed.
                # Keep the delegated fence until external intervention.
                time.sleep(1.0)
                continue
        except BaseException:
            time.sleep(1.0)
            continue
        time.sleep(0.02)


def _terminal_from(prepared: Mapping[str, Any], **updates) -> Dict[str, Any]:  # noqa: ANN003
    terminal = dict(prepared)
    sandbox = _drain_external_container(
        prepared, capture_logs=updates.get("outcome") != "exit")
    if sandbox is not None:
        terminal["sandbox"] = sandbox
    terminal.update(updates)
    terminal["state"] = "terminal"
    terminal["group_drained"] = True
    terminal["finished_at_unix"] = time.time()
    if prepared.get("capture_stdout_ref") is not None:
        try:
            for stream, fd in (("stdout", 1), ("stderr", 2)):
                identity = _capture_identity(
                    Path(prepared[f"capture_{stream}_ref"]), fd)
                for field, value in identity.items():
                    terminal[f"capture_{stream}_{field}"] = value
        except Exception as error:
            for stream in ("stdout", "stderr"):
                for field in ("sha256", "bytes", "device", "inode"):
                    terminal.pop(f"capture_{stream}_{field}", None)
            # Process-tree terminal authority remains publishable even when
            # output identity is not.  Recovery will keep the exact runner_call
            # but take the unknown-usage fail-closed path.
            terminal["capture_error"] = (
                f"{type(error).__name__}: {error}"[:500] or "capture identity failed")
    return terminal


def _guardian_main(*, spec_fd: int, owner_fd: int, owner_pid: int,
                   fence_fd: int) -> int:
    # Do not use fatal PR_SET_PDEATHSIG: the guardian must survive its owner to
    # clean the tree, fsync the receipt, and only then release the flock dup.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGUSR1):
        signal.signal(sig, _guardian_signal)
    for fd in (spec_fd, owner_fd, fence_fd):
        if fd >= 0:
            os.set_inheritable(fd, False)
    spec = _read_spec_fd(spec_fd)
    prepared = spec["prepared"]
    receipt_path = Path(spec["receipt_path"])
    heartbeat_path = receipt_path.with_name(
        f"heartbeat-{prepared['operation_id']}.json")
    try:
        pdeath_event = _arm_parent_death_signal(owner_pid)
    except BaseException as error:
        terminal = _terminal_from(
            prepared, outcome="spawn_failed", returncode=None,
            started_at_unix=None, term_sent=False, kill_sent=False,
            spawn_error=f"pdeathsig:{type(error).__name__}")
        _write_receipt_until_durable(receipt_path, terminal)
        _release_fence(fence_fd)
        return 0
    try:
        _set_subreaper()
        if _proc_info(os.getpid()) is None:
            raise ExecutionSupervisorError("guardian self /proc identity 不可读")
        _children(os.getpid())
    except BaseException as error:
        terminal = _terminal_from(
            prepared, outcome="spawn_failed", returncode=None,
            started_at_unix=None, term_sent=False, kill_sent=False,
            spawn_error=f"subreaper/procfs:{type(error).__name__}")
        _write_receipt_until_durable(receipt_path, terminal)
        _release_fence(fence_fd)
        return 0
    event = pdeath_event or _owner_event(owner_fd, 0.0)
    if event is not None:
        terminal = _terminal_from(
            prepared, outcome=event, returncode=None,
            started_at_unix=None, term_sent=False, kill_sent=False)
        _write_receipt_until_durable(receipt_path, terminal)
        _release_fence(fence_fd)
        return 0

    gate_read, gate_write = os.pipe2(getattr(os, "O_CLOEXEC", 0))
    proc: Optional[subprocess.Popen] = None
    try:
        os.lseek(spec_fd, 0, os.SEEK_SET)
        payload_args = [
            sys.executable, "-I", os.path.abspath(__file__), _PAYLOAD_FLAG,
            "--spec-fd", str(spec_fd), "--gate-fd", str(gate_read),
        ]
        target_fds = tuple(int(fd) for fd in spec.get("target_pass_fds", []))
        proc = subprocess.Popen(
            payload_args, stdin=None, stdout=None, stderr=None,
            close_fds=True, pass_fds=(spec_fd, gate_read, *target_fds),
            start_new_session=True)
        # The launcher inherited the explicit target capabilities.  Guardian
        # copies must close immediately: retaining a pipe writer or lock fd
        # would change pass_fds EOF/unlock semantics and could force timeout.
        for fd in set(target_fds):
            try:
                os.close(fd)
            except OSError:
                pass
        # The launcher inherited its own spec copy.  Closing the guardian copy
        # prevents the eventual same-UID workload from reading target env via
        # /proc/<guardian>/fd after it execs.
        os.close(spec_fd)
        spec_fd = -1
        os.close(gate_read)
        gate_read = -1
        info = _proc_info(proc.pid)
        if info is None:
            raise RuntimeError("payload launcher identity 不可读")
        started_at = time.time()
        raw_deadline = spec["deadline_monotonic_s"]
        deadline_monotonic = (
            None if raw_deadline is None else float(raw_deadline))
        deadline_at_unix = (
            None if deadline_monotonic is None else
            started_at + max(0.0, deadline_monotonic - time.monotonic()))
        activity = _execution_activity_sample(proc)
        running = dict(prepared)
        running.update({
            "state": "running", "helper_pid": os.getpid(),
            "helper_start_ticks": _proc_info(os.getpid())[0],
            "payload_pid": proc.pid, "payload_start_ticks": info[0],
            "initial_pgid": proc.pid, "started_at_unix": started_at,
            "deadline_at_unix": deadline_at_unix,
            "heartbeat_ref": str(heartbeat_path),
            "guardian_heartbeat_seq": 0,
            "guardian_heartbeat_at_unix": started_at,
            "last_activity_at_unix": started_at,
            **activity,
        })
        _write_receipt_until_durable(receipt_path, running)
        _atomic_write_heartbeat(heartbeat_path, running)
        event = _owner_event(owner_fd, 0.0)
        if (event is None and deadline_monotonic is not None
                and time.monotonic() >= deadline_monotonic):
            outcome = "timeout"
        elif event is None:
            _write_all(gate_write, b"G")
            outcome = None
        else:
            outcome = event
        os.close(gate_write)
        gate_write = -1
    except BaseException as error:
        if gate_write >= 0:
            try:
                os.close(gate_write)
            except OSError:
                pass
            gate_write = -1
        if proc is not None:
            try:
                leader_info = _proc_info(proc.pid)
                pre_cleanup_errors = 0
            except BaseException:
                leader_info = None
                pre_cleanup_errors = 1
            leader_start = leader_info[0] if leader_info is not None else "-1"
            term_sent, kill_sent, max_descendants, signal_errors = _terminate_tree(
                proc, leader_start=leader_start,
                grace_s=float(spec["term_grace_s"]))
            signal_errors += pre_cleanup_errors
        else:
            term_sent = kill_sent = False
            max_descendants = 0
            signal_errors = 0
        terminal = _terminal_from(
            prepared, outcome="spawn_failed", returncode=None,
            started_at_unix=None, term_sent=term_sent, kill_sent=kill_sent,
            max_descendants=max_descendants,
            signal_error_count=signal_errors,
            spawn_error=type(error).__name__)
        _fsync_output_fds()
        _write_receipt_until_durable(receipt_path, terminal)
        _release_fence(fence_fd)
        return 0
    finally:
        if gate_read >= 0:
            try:
                os.close(gate_read)
            except OSError:
                pass

    leader_start = running["payload_start_ticks"]
    raw_deadline = spec["deadline_monotonic_s"]
    deadline = None if raw_deadline is None else float(raw_deadline)
    last_sample = (
        running["activity_cpu_ticks"], running["activity_output_bytes"],
        running["activity_descendant_count"])
    next_sample = time.monotonic() + _ACTIVITY_SAMPLE_INTERVAL_S
    next_heartbeat = time.monotonic() + _HEARTBEAT_INTERVAL_S
    if outcome is None:
        while True:
            wait_s = (0.05 if deadline is None else
                      min(0.05, max(0.0, deadline - time.monotonic())))
            event = _owner_event(owner_fd, wait_s)
            if event is not None:
                outcome = event
                break
            returncode = _poll_leader(proc)
            if returncode is not None:
                outcome = ("exit" if _all_children_drained(proc)
                           else "lingering_descendant")
                break
            now_monotonic = time.monotonic()
            if now_monotonic >= next_sample:
                sample = _execution_activity_sample(proc)
                sample_key = (
                    sample["activity_cpu_ticks"], sample["activity_output_bytes"],
                    sample["activity_descendant_count"])
                if sample_key != last_sample:
                    running["last_activity_at_unix"] = time.time()
                    last_sample = sample_key
                running.update(sample)
                next_sample = now_monotonic + _ACTIVITY_SAMPLE_INTERVAL_S
            if now_monotonic >= next_heartbeat:
                running["guardian_heartbeat_seq"] += 1
                running["guardian_heartbeat_at_unix"] = time.time()
                _atomic_write_heartbeat(heartbeat_path, running)
                next_heartbeat = now_monotonic + _HEARTBEAT_INTERVAL_S
            if deadline is not None and now_monotonic >= deadline:
                outcome = "timeout"
                break

    if outcome == "exit":
        term_sent = kill_sent = False
        max_descendants = 0
        signal_errors = 0
    else:
        _await_external_registration_or_runner_exit(running, proc)
        term_sent, kill_sent, max_descendants, signal_errors = _terminate_tree(
            proc, leader_start=leader_start,
            grace_s=float(spec["term_grace_s"]))
    _poll_leader(proc)
    if not _tree_empty(os.getpid(), proc):
        # This path should be unreachable because _terminate_tree loops until
        # empty; preserve the fence if a future refactor violates it.
        while True:
            time.sleep(1.0)
    _fsync_output_fds()
    try:
        final_sample = _execution_activity_sample(proc)
    except ExecutionSupervisorError:
        final_sample = {
            "activity_cpu_ticks": running["activity_cpu_ticks"],
            "activity_output_bytes": running["activity_output_bytes"],
            "activity_descendant_count": 0,
        }
    final_key = (
        final_sample["activity_cpu_ticks"], final_sample["activity_output_bytes"],
        final_sample["activity_descendant_count"])
    if final_key != last_sample:
        running["last_activity_at_unix"] = time.time()
    running.update(final_sample)
    running["guardian_heartbeat_seq"] += 1
    running["guardian_heartbeat_at_unix"] = time.time()
    terminal = _terminal_from(
        running, outcome=outcome, returncode=proc.returncode,
        term_sent=term_sent, kill_sent=kill_sent,
        max_descendants=max_descendants,
        signal_error_count=signal_errors)
    _atomic_write_heartbeat(heartbeat_path, terminal)
    _write_receipt_until_durable(receipt_path, terminal)
    _release_fence(fence_fd)
    return 0


def _payload_main(*, spec_fd: int, gate_fd: int) -> int:
    spec = _read_spec_fd(spec_fd)
    token = os.read(gate_fd, 1)
    os.close(gate_fd)
    os.close(spec_fd)
    if token != b"G":
        return 125
    cwd = spec.get("cwd")
    if cwd is not None:
        os.chdir(cwd)
    argv = spec["argv"]
    os.execvpe(argv[0], argv, spec["env"])
    return 126


def _parse_helper_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("mode", choices=[_HELPER_FLAG, _PAYLOAD_FLAG])
    parser.add_argument("--spec-fd", type=int, required=True)
    parser.add_argument("--owner-fd", type=int)
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--fence-fd", type=int, default=-1)
    parser.add_argument("--gate-fd", type=int)
    return parser.parse_args(argv)


def _main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_helper_args(argv)
    if args.mode == _HELPER_FLAG:
        if args.owner_fd is None or args.owner_pid is None or args.owner_pid <= 0:
            raise SystemExit("guardian 缺/非法 --owner-fd/--owner-pid")
        return _guardian_main(
            spec_fd=args.spec_fd, owner_fd=args.owner_fd, owner_pid=args.owner_pid,
            fence_fd=args.fence_fd)
    if args.gate_fd is None:
        raise SystemExit("payload 缺 --gate-fd")
    return _payload_main(spec_fd=args.spec_fd, gate_fd=args.gate_fd)


if __name__ == "__main__":
    raise SystemExit(_main())
