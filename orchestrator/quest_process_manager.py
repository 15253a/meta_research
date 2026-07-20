"""Local Web-owned process capability for published quests.

The manager deliberately owns no research state.  It can only spawn the
canonical ``orchestrator.run`` entry point with constructor-frozen arguments,
observe the existing instance lease, and signal process groups for which this
exact manager still holds the in-memory :class:`subprocess.Popen` capability.
PID files are never read and an owner observed after manager restart is never
treated as signal authority.
"""
from __future__ import annotations

import math
import os
import platform
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Set

import yaml

from .instance_lease import read_instance_status
from .qualification_firewall import (
    CONTRACT_RELATIVE_PATH,
    QualificationFirewallError,
    _read_regular,
    _strict_json,
)
from .quest_registry import Quest, QuestRegistry
from .quest_runtime_profiles import (
    QuestRuntimeSettings,
    RuntimeSettingsCorruptError,
    public_options,
)


_IDEMPOTENCY_KEY_LENGTH = 32
_MAX_OPERATION_KEYS_PER_QUEST = 1024
_MAX_LOG_BYTES_BEFORE_TRUNCATE = 8 * 1024 * 1024
_TERMINATE_TIMEOUT_S = 10.0
_KILL_TIMEOUT_S = 5.0
_GROUP_POLL_INTERVAL_S = 0.05
_START_OBSERVE_TIMEOUT_S = 0.35
_START_OBSERVE_POLL_INTERVAL_S = 0.02
_LOG_REF = "state/web-owner.log"
_MAX_PUBLIC_LOG_TAIL_BYTES = 32 * 1024
_MAX_PUBLIC_LOG_CHARS = 16 * 1024
_LAUNCHER_JOIN_TIMEOUT_S = 5.0
_LAUNCHER_STOP = object()
_RUNTIME_RESTART_POLL_INTERVAL_S = 0.05
_RUNTIME_RESTART_JOIN_TIMEOUT_S = 5.0
_MAX_AUTOMATIC_BOUND_PROFILE_RECOVERIES = 1
_GPU_DISCOVERY_TIMEOUT_S = 3.0
_MAX_GPU_DISCOVERY_OUTPUT_BYTES = 64 * 1024
_MAX_GPU_DISCOVERY_DEVICES = 64
_MAX_GPU_DEVICE_INDEX = 4095
_MAX_GPU_MEMORY_MIB = 16 * 1024 * 1024
_GPU_INTEGER_RE = re.compile(r"(?:0|[1-9][0-9]*)")
_GPU_UUID_LIKE_RE = re.compile(r"GPU-[0-9A-Fa-f-]{16,}")


class QuestProcessManagerError(RuntimeError):
    """The Web owner process capability could not complete an operation."""


class QuestProcessUnavailableError(QuestProcessManagerError):
    """A quest cannot be safely started by this manager identity/config."""


class QuestProcessManagerClosedError(QuestProcessManagerError):
    """A mutating operation was attempted after manager shutdown began."""


@dataclass
class _ManagedChild:
    process: subprocess.Popen
    process_group_id: int
    start_key: str
    owner_intent_revision: int
    applied_runtime_revision: int
    applied_runtime_record_sha256: Optional[str]
    terminating: bool = False
    stop_requested: bool = False


@dataclass
class _QuestSlot:
    lock: threading.RLock = field(default_factory=threading.RLock)
    child: Optional[_ManagedChild] = None
    start_keys: Set[str] = field(default_factory=set)
    terminate_keys: Set[str] = field(default_factory=set)
    runtime_restart_keys: Set[str] = field(default_factory=set)
    runtime_restart_scheduled_keys: Set[str] = field(default_factory=set)
    runtime_restart_requested: bool = False
    runtime_restart_target_revision: Optional[int] = None
    runtime_restart_target_sha256: Optional[str] = None
    runtime_restart_start_key: Optional[str] = None
    runtime_restart_watcher: Optional[threading.Thread] = None
    runtime_restart_error: Optional[str] = None
    bound_profile_recovery_attempts: int = 0


@dataclass
class _SpawnRequest:
    argv: list[str]
    kwargs: Dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    process: Optional[subprocess.Popen] = None
    error: Optional[BaseException] = None


def _idempotency_key(value: object) -> str:
    if (not isinstance(value, str) or len(value) != _IDEMPOTENCY_KEY_LENGTH
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError("idempotency_key 须为 32 位小写 hex")
    return value


def _nonnegative_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} 须为非负整数")
    return value


def _positive_float(value: object, *, label: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or float(value) < 0.01):
        raise ValueError(f"{label} 须为 >= 0.01 的有限数")
    return float(value)


def _local_gpu_devices() -> list[Dict[str, Any]]:
    """Return a bounded, UUID-free inventory from one controlled probe."""
    binary = shutil.which("nvidia-smi", path=os.defpath)
    if binary is None or not os.path.isabs(binary):
        raise ValueError("nvidia-smi 不可用")
    completed = subprocess.run(
        [binary, "--query-gpu=index,name,memory.total",
         "--format=csv,noheader,nounits"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_GPU_DISCOVERY_TIMEOUT_S,
        check=False,
        start_new_session=True,
        env={
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        })
    stdout = getattr(completed, "stdout", None)
    stderr = getattr(completed, "stderr", None)
    returncode = getattr(completed, "returncode", None)
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        raise ValueError("nvidia-smi 输出类型非法")
    if len(stdout) + len(stderr) > _MAX_GPU_DISCOVERY_OUTPUT_BYTES:
        raise ValueError("nvidia-smi 输出超过上限")
    if returncode != 0:
        raise ValueError("nvidia-smi 探测失败")
    try:
        text = stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("nvidia-smi 输出不是 UTF-8") from error

    devices: list[Dict[str, Any]] = []
    seen: set[int] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            raise ValueError("nvidia-smi GPU 行字段数非法")
        raw_index, model, raw_memory = parts
        if (_GPU_INTEGER_RE.fullmatch(raw_index) is None
                or _GPU_INTEGER_RE.fullmatch(raw_memory) is None):
            raise ValueError("nvidia-smi GPU 数值格式非法")
        index = int(raw_index)
        memory_mib = int(raw_memory)
        try:
            model_bytes = model.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("nvidia-smi GPU 型号编码非法") from error
        if (not 0 <= index <= _MAX_GPU_DEVICE_INDEX or index in seen
                or not 1 <= memory_mib <= _MAX_GPU_MEMORY_MIB
                or not 1 <= len(model_bytes) <= 256
                or any(ord(char) < 0x20 or ord(char) == 0x7F
                       for char in model)
                or "/" in model or "\\" in model
                or _GPU_UUID_LIKE_RE.search(model) is not None):
            raise ValueError("nvidia-smi GPU 身份/型号/显存非法")
        seen.add(index)
        devices.append({
            "index": index,
            "model": model,
            "memory_bytes": memory_mib * 1024 * 1024,
        })
        if len(devices) > _MAX_GPU_DISCOVERY_DEVICES:
            raise ValueError("nvidia-smi GPU 数量超过上限")
    if not devices:
        raise ValueError("nvidia-smi 未返回 GPU")
    return sorted(devices, key=lambda row: row["index"])


class QuestProcessManager:
    """Spawn and stop at most one locally-owned process group per quest."""

    def __init__(
            self, registry: QuestRegistry, system_root: Path | str, *,
            python_executable: Path | str = sys.executable,
            connector_profile: Optional[Path | str] = None,
            no_outbound: bool = True,
            max_cycles: int = 100,
            poll_interval_s: float = 1.0):
        self.registry = registry
        self.system_root = Path(system_root).resolve(strict=True)
        executable = Path(python_executable)
        if not executable.is_absolute():
            raise ValueError("python_executable 须为绝对路径")
        try:
            executable_info = executable.stat()
        except OSError as error:
            raise ValueError("python_executable 不可读") from error
        if (not stat.S_ISREG(executable_info.st_mode)
                or not os.access(executable, os.X_OK)):
            raise ValueError("python_executable 须为可执行常规文件")
        if not isinstance(no_outbound, bool):
            raise ValueError("no_outbound 须为 bool")
        if no_outbound and connector_profile is not None:
            raise ValueError("no_outbound 与 connector_profile 不得同时提供")
        self.python_executable = str(executable)
        self.connector_profile = (
            None if connector_profile is None
            else os.path.abspath(os.fspath(connector_profile)))
        self.no_outbound = no_outbound
        self.max_cycles = _nonnegative_integer(max_cycles, label="max_cycles")
        self.poll_interval_s = _positive_float(
            poll_interval_s, label="poll_interval_s")
        self._guard = threading.RLock()
        self._slots: Dict[str, _QuestSlot] = {}
        self._closed = False
        self._close_complete = False
        self._health_cache: Optional[tuple[float, Dict[str, Any]]] = None
        # Linux PR_SET_PDEATHSIG is tied to the thread which created the
        # child, not merely to the containing process.  HTTP handler threads
        # are short-lived, so spawning there would SIGTERM a healthy owner as
        # soon as its start response completed.  Every child is therefore
        # forked by this one manager-lifetime broker thread.
        self._launch_queue: queue.Queue[Any] = queue.Queue()
        self._launcher_thread = threading.Thread(
            target=self._launcher_loop, daemon=True,
            name="meta-research-owner-launcher")
        self._launcher_thread.start()

    def _launcher_loop(self) -> None:
        while True:
            request = self._launch_queue.get()
            if request is _LAUNCHER_STOP:
                return
            if not isinstance(request, _SpawnRequest):
                continue
            try:
                request.process = subprocess.Popen(
                    request.argv, **request.kwargs)
            except BaseException as error:
                request.error = error
            finally:
                request.done.set()

    def _spawn(self, argv: list[str], **kwargs: Any) -> subprocess.Popen:
        request = _SpawnRequest(argv=list(argv), kwargs=dict(kwargs))
        self._launch_queue.put(request)
        while not request.done.wait(timeout=0.1):
            if not self._launcher_thread.is_alive():
                raise QuestProcessManagerError(
                    "quest owner 常驻启动器意外退出")
        if request.error is not None:
            raise request.error
        if request.process is None:
            raise QuestProcessManagerError("quest owner 启动器未返回进程能力")
        return request.process

    def runtime_health(self) -> Dict[str, Any]:
        """Return a path-free, read-only readiness projection for the Web UI."""
        with self._guard:
            cached = self._health_cache
            if cached is not None and time.monotonic() - cached[0] < 10.0:
                return dict(cached[1])
        checks = {
            "supported_platform": False,
            "codex_cli": False,
            "query_cli": False,
            "docker_engine": False,
            "docker_daemon": False,
            "sandbox_image": False,
        }
        detail = "runtime policy unavailable"
        disk_free = None
        try:
            disk_free = shutil.disk_usage(self.registry.root).free
        except OSError:
            pass
        try:
            policy = yaml.safe_load(
                (self.system_root / "policies" / "policy.yaml").read_text(
                    encoding="utf-8"))
            sandbox = policy["execution"]["sandbox"]
            engine = str(sandbox["engine_path"])
            engine_host = str(sandbox["engine_host"])
            image = str(sandbox["image"])
            image_id = str(sandbox["image_id"])
            expected_arch = str(sandbox["seccomp_bpf_arch"])
            machine = platform.machine().lower()
            live_arch = "amd64" if machine in {"x86_64", "amd64"} else machine
            checks["supported_platform"] = (
                platform.system() == "Linux" and live_arch == expected_arch)
            checks["docker_engine"] = (
                os.path.isabs(engine) and os.path.isfile(engine)
                and os.access(engine, os.X_OK))
            codex_bin = os.environ.get("METARESEARCH_CODEX_BIN", "codex-chatgpt")
            query_bin = os.environ.get(
                "METARESEARCH_QUERY_CODEX_BIN", "/usr/local/bin/codex")
            checks["codex_cli"] = shutil.which(codex_bin) is not None
            checks["query_cli"] = shutil.which(query_bin) is not None
            if checks["docker_engine"]:
                completed = subprocess.run(
                    [engine, "--host", engine_host, "image", "inspect",
                     "--format", "{{.Id}}", image],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, timeout=5,
                    check=False, env={
                        "PATH": "/usr/local/bin:/usr/bin:/bin",
                        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                    })
                checks["docker_daemon"] = completed.returncode == 0
                checks["sandbox_image"] = (
                    checks["docker_daemon"]
                    and completed.stdout.strip() == image_id)
            detail = "ready" if all(checks.values()) else "dependency_check_failed"
        except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError,
                yaml.YAMLError):
            detail = "runtime_policy_or_probe_failed"
        result = {
            "ready": all(checks.values()),
            "checks": checks,
            "detail": detail,
            "disk_free_bytes": disk_free,
        }
        with self._guard:
            self._health_cache = (time.monotonic(), result)
        return dict(result)

    def _runtime_gpu_policy(self) -> tuple[list[int], int, float]:
        policy = yaml.safe_load(
            (self.system_root / "policies" / "policy.yaml").read_text(
                encoding="utf-8"))
        resources = policy["resources"]
        indices = resources["allowed_device_indices"]
        requested = resources["gpus"]
        memory_gb = resources["gpu_mem_gb"]
        if (not isinstance(indices, list)
                or any(isinstance(item, bool) or not isinstance(item, int)
                       or not 0 <= item <= _MAX_GPU_DEVICE_INDEX
                       for item in indices)
                or indices != sorted(set(indices))
                or not isinstance(requested, int)
                or isinstance(requested, bool)
                or not 0 <= requested <= len(indices)
                or isinstance(memory_gb, bool)
                or not isinstance(memory_gb, (int, float))
                or not math.isfinite(float(memory_gb))
                or float(memory_gb) < 0):
            raise ValueError("resources GPU option contract 非法")
        if requested == 0:
            if indices or float(memory_gb) != 0:
                raise ValueError("CPU resources GPU option contract 非法")
        elif float(memory_gb) <= 0:
            raise ValueError("GPU 显存下限须为正数")
        return list(indices), requested, float(memory_gb)

    def runtime_profile_legacy_gpu_count(self) -> int:
        """Return the private fixed count used only by legacy v2 profiles."""
        try:
            _indices, requested, _memory_gb = self._runtime_gpu_policy()
            return requested
        except (KeyError, TypeError, ValueError, OSError,
                yaml.YAMLError) as error:
            raise QuestProcessUnavailableError(
                "legacy runtime GPU count 不可用") from error

    def runtime_profile_options(self) -> Dict[str, Any]:
        """Intersect live capable GPUs with policy and return an exact catalog."""
        try:
            indices, requested, memory_gb = self._runtime_gpu_policy()
            if requested == 0:
                return public_options()
            required_memory = math.ceil(float(memory_gb) * (1024 ** 3))
            allowed = set(indices)
            devices = [
                row for row in _local_gpu_devices()
                if row["index"] in allowed
                and row["memory_bytes"] >= required_memory
            ]
            if not devices:
                raise ValueError("当前没有满足 policy/显存下限的可信 GPU")
            return public_options(
                allowed_gpu_indices=[row["index"] for row in devices],
                requested_gpu_count=requested,
                exact_multi_gpu=True,
                gpu_device_labels=devices)
        except (KeyError, TypeError, ValueError, OSError,
                subprocess.SubprocessError, yaml.YAMLError) as error:
            raise QuestProcessUnavailableError(
                "runtime GPU option catalog 不可用") from error

    def _slot(self, quest_id: str, *, require_open: bool) -> _QuestSlot:
        with self._guard:
            if require_open and self._closed:
                raise QuestProcessManagerClosedError(
                    "quest process manager 已关闭")
            slot = self._slots.get(quest_id)
            if slot is None:
                slot = _QuestSlot()
                self._slots[quest_id] = slot
            return slot

    def _ensure_open(self) -> None:
        with self._guard:
            if self._closed:
                raise QuestProcessManagerClosedError(
                    "quest process manager 已关闭")

    @staticmethod
    def _check_key_capacity(keys: Set[str], key: str) -> None:
        if key not in keys and len(keys) >= _MAX_OPERATION_KEYS_PER_QUEST:
            raise QuestProcessUnavailableError(
                "quest Web owner 幂等操作已达安全上限")

    @staticmethod
    def _group_alive(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Loss of signal authority is not evidence that the group exited.
            return True

    @staticmethod
    def _instance_status(quest: Quest) -> Dict[str, Any]:
        return read_instance_status(quest.work_root)

    def _public_status_locked(
            self, quest: Quest, slot: _QuestSlot) -> Dict[str, Any]:
        child = slot.child
        child_code = None
        managed_group_alive = False
        if child is not None:
            child_code = child.process.poll()
            managed_group_alive = (
                child_code is None or self._group_alive(child.process_group_id))

        instance = self._instance_status(quest)
        safe_owner_state = instance.get("state")
        if safe_owner_state not in {
                "starting", "ready", "running", "paused", "stopping", "stopped"}:
            safe_owner_state = None
        heartbeat_age = instance.get("heartbeat_age_s")
        if (isinstance(heartbeat_age, bool)
                or not isinstance(heartbeat_age, (int, float))
                or not math.isfinite(float(heartbeat_age))
                or float(heartbeat_age) < 0):
            heartbeat_age = None

        def runtime_status(payload: Dict[str, Any]) -> Dict[str, Any]:
            live_child = child if child is not None and managed_group_alive else None
            payload.update({
                "runtime_profile_restart_pending": bool(
                    slot.runtime_restart_requested),
                "applied_runtime_profile_revision": (
                    None if live_child is None
                    else live_child.applied_runtime_revision),
                "runtime_profile_restart_error": slot.runtime_restart_error,
            })
            return payload

        if child is not None and managed_group_alive:
            state = (
                "stopping" if child.terminating or child_code is not None
                else ("running" if instance.get("active") is True else "starting"))
            return runtime_status({
                "quest_id": quest.quest_id,
                "state": state,
                "active": True,
                "managed_by_web": True,
                "terminable": True,
                "exit_code": child_code,
                "owner_state": safe_owner_state,
                "heartbeat_age_s": heartbeat_age,
                "log_ref": _LOG_REF,
            })

        if instance.get("lock_held") is True:
            return runtime_status({
                "quest_id": quest.quest_id,
                "state": (
                    "external_active" if instance.get("active") is True
                    else "external_locked"),
                "active": True,
                "managed_by_web": False,
                "terminable": False,
                "exit_code": None,
                "owner_state": safe_owner_state,
                "heartbeat_age_s": heartbeat_age,
                "log_ref": _LOG_REF,
            })

        if child is not None:
            return runtime_status({
                "quest_id": quest.quest_id,
                "state": "stopped" if child.stop_requested else "exited",
                "active": False,
                "managed_by_web": True,
                "terminable": False,
                "exit_code": child_code,
                "owner_state": None,
                "heartbeat_age_s": None,
                "log_ref": _LOG_REF,
            })
        return runtime_status({
            "quest_id": quest.quest_id,
            "state": "inactive",
            "active": False,
            "managed_by_web": False,
            "terminable": False,
            "exit_code": None,
            "owner_state": None,
            "heartbeat_age_s": None,
            "log_ref": _LOG_REF,
        })

    @staticmethod
    def _assert_local_owner_identity(quest: Quest) -> None:
        try:
            work_info = quest.work_root.lstat()
        except OSError as error:
            raise QuestProcessUnavailableError(
                "quest work-root 不可用") from error
        if (not stat.S_ISDIR(work_info.st_mode) or stat.S_ISLNK(work_info.st_mode)
                or work_info.st_uid != os.geteuid()):
            raise QuestProcessUnavailableError(
                "Web owner 进程 UID 与 quest work-root owner 不匹配")
        if quest.qualification_profile_id is None:
            return
        try:
            raw = _read_regular(
                quest.work_root / CONTRACT_RELATIVE_PATH,
                label="qualification contract",
                expected_owner=work_info.st_uid, expected_mode=0o400)
            contract = _strict_json(raw, label="qualification contract")
        except QualificationFirewallError as error:
            raise QuestProcessUnavailableError(
                "qualification contract 本地身份无法核验") from error
        research_uid = contract.get("research_uid")
        if (isinstance(research_uid, bool) or not isinstance(research_uid, int)
                or research_uid < 0):
            raise QuestProcessUnavailableError(
                "qualification contract research_uid 非法")
        if research_uid != os.geteuid():
            raise QuestProcessUnavailableError(
                "qualification quest 要求 Web owner 以合同 research_uid 运行；"
                "禁止 root/sudo 回退")

    @staticmethod
    def _open_log(quest: Quest) -> int:
        state = quest.work_root / "state"
        try:
            state_info = state.lstat()
        except OSError as error:
            raise QuestProcessUnavailableError("quest state 目录不可用") from error
        if (not stat.S_ISDIR(state_info.st_mode) or stat.S_ISLNK(state_info.st_mode)
                or state_info.st_uid != os.geteuid()
                or stat.S_IMODE(state_info.st_mode) != 0o700):
            raise QuestProcessUnavailableError("quest state 目录身份非法")
        path = state / "web-owner.log"
        common = (os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
                  | getattr(os, "O_NOFOLLOW", 0))
        try:
            fd = os.open(path, common | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                fd = os.open(path, common)
            except OSError as error:
                raise QuestProcessUnavailableError(
                    "quest Web owner log 不可安全打开") from error
        except OSError as error:
            raise QuestProcessUnavailableError(
                "quest Web owner log 不可创建") from error
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise QuestProcessUnavailableError(
                    "quest Web owner log 身份/权限非法")
            if info.st_size > _MAX_LOG_BYTES_BEFORE_TRUNCATE:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_END)
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _validate_runtime_profile_record(
            quest: Quest, current: Dict[str, Any]) -> tuple[int, Optional[str]]:
        revision = current.get("revision")
        digest = current.get("record_sha256")
        if (current.get("quest_id") != quest.quest_id
                or isinstance(revision, bool) or not isinstance(revision, int)
                or revision < 0
                or (digest is not None and not isinstance(digest, str))
                or ((revision == 0) != (digest is None))):
            raise QuestProcessUnavailableError(
                "quest runtime profile identity 非法")
        return revision, digest

    @classmethod
    def _runtime_profile_current(cls, quest: Quest) -> Dict[str, Any]:
        try:
            current = QuestRuntimeSettings(
                quest.work_root, quest.quest_id).current()
        except (OSError, ValueError, RuntimeSettingsCorruptError) as error:
            raise QuestProcessUnavailableError(
                "quest runtime profile 无法严格核验") from error
        cls._validate_runtime_profile_record(quest, current)
        return current

    @classmethod
    def _runtime_profile_launch(
            cls, quest: Quest) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Return ``(applied, desired)`` with durable cycle binding priority."""
        try:
            settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
            desired = settings.current()
            bound = settings.bound_cycle_profile()
        except (OSError, ValueError, RuntimeSettingsCorruptError) as error:
            raise QuestProcessUnavailableError(
                "quest runtime profile/cycle binding 无法严格核验") from error
        cls._validate_runtime_profile_record(quest, desired)
        if bound is not None:
            cls._validate_runtime_profile_record(quest, bound)
        return (bound if bound is not None else desired), desired

    def _argv(self, quest: Quest,
              runtime_profile: Optional[Dict[str, Any]] = None) -> list[str]:
        current = (
            self._runtime_profile_launch(quest)[0]
            if runtime_profile is None else runtime_profile)
        revision = int(current["revision"])
        digest = current["record_sha256"]
        argv = [
            self.python_executable,
            "-m", "orchestrator.web_owner_child",
            "--expected-parent-pid", str(os.getpid()),
            "--",
            "--system-root", str(self.system_root),
            "--work-root", str(quest.work_root),
            "--quest-id", quest.quest_id,
            "--runtime-profile-revision", str(revision),
            "--max-cycles", str(self.max_cycles),
            "--poll-interval-s", str(self.poll_interval_s),
        ]
        if digest is not None:
            argv.extend(["--runtime-profile-record-sha256", digest])
        if self.no_outbound:
            argv.append("--no-outbound")
        elif self.connector_profile is not None:
            argv.extend(["--connector-profile", self.connector_profile])
        return argv

    def _spawn_managed_child(
            self, quest: Quest, *, start_key: str,
            runtime_profile: Dict[str, Any],
            owner_intent_revision: int) -> _ManagedChild:
        """Spawn one exact profile/owner generation, fenced on both sides."""
        self._assert_local_owner_identity(quest)
        settings = QuestRuntimeSettings(quest.work_root, quest.quest_id)
        try:
            authorized_before = settings.owner_start_generation_authorized(
                owner_intent_revision)
        except (OSError, ValueError, RuntimeSettingsCorruptError) as error:
            raise QuestProcessUnavailableError(
                "quest owner generation 无法在 Popen 前严格核验") from error
        if not authorized_before:
            raise QuestProcessUnavailableError(
                "quest owner generation 已被 durable stop fence 取消")
        log_fd = self._open_log(quest)
        try:
            process = self._spawn(
                self._argv(quest, runtime_profile),
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                cwd=str(self.system_root),
                close_fds=True,
                start_new_session=True,
                shell=False,
            )
        finally:
            os.close(log_fd)
        child = _ManagedChild(
            process=process, process_group_id=process.pid,
            start_key=start_key,
            owner_intent_revision=owner_intent_revision,
            applied_runtime_revision=int(runtime_profile["revision"]),
            applied_runtime_record_sha256=runtime_profile["record_sha256"],
        )
        try:
            authorized_after = settings.owner_start_generation_authorized(
                owner_intent_revision)
        except (OSError, ValueError, RuntimeSettingsCorruptError) as error:
            try:
                self._terminate_child(child)
            except BaseException as stop_error:
                raise QuestProcessManagerError(
                    "Popen 后 owner generation 核验失败且 child 无法收口") \
                    from stop_error
            raise QuestProcessUnavailableError(
                "quest owner generation 无法在 Popen 后严格核验") from error
        if not authorized_after:
            try:
                self._terminate_child(child)
            except BaseException as error:
                raise QuestProcessManagerError(
                    "durable stop fence 后启动的 child 无法收口") from error
            raise QuestProcessUnavailableError(
                "quest owner generation 在 Popen 期间被 durable stop fence 取消")
        return child

    def _refresh_runtime_restart_after_spawn_locked(
            self, quest: Quest, slot: _QuestSlot, *, start_key: str) -> None:
        """Close snapshot->spawn races with one strict post-spawn re-read."""
        child = slot.child
        if child is None:
            raise QuestProcessManagerError("runtime profile spawn 后缺 child capability")
        launch_now, desired_now = self._runtime_profile_launch(quest)
        applied_identity = (
            child.applied_runtime_revision,
            child.applied_runtime_record_sha256)
        launch_identity = self._validate_runtime_profile_record(
            quest, launch_now)
        desired_identity = self._validate_runtime_profile_record(
            quest, desired_now)
        needs_followup = (
            applied_identity != launch_identity
            or applied_identity != desired_identity)
        slot.runtime_restart_requested = needs_followup
        slot.runtime_restart_target_revision = (
            desired_now["revision"] if needs_followup else None)
        slot.runtime_restart_target_sha256 = (
            desired_now["record_sha256"] if needs_followup else None)
        slot.runtime_restart_start_key = start_key if needs_followup else None
        slot.runtime_restart_error = None
        if needs_followup:
            self._start_runtime_restart_watcher_locked(quest, slot)
        else:
            try:
                settings = QuestRuntimeSettings(
                    quest.work_root, quest.quest_id)
                applied = settings.record(
                    child.applied_runtime_revision,
                    child.applied_runtime_record_sha256)
                settings.settle_applied_runtime_updates(applied)
            except (OSError, ValueError, RuntimeSettingsCorruptError) as error:
                # The spawned generation remains valid; retain a safe status
                # diagnostic and leave accepted receipts replayable instead of
                # falsely declaring their asynchronous side effect settled.
                slot.runtime_restart_error = type(error).__name__

    def start(self, quest_id: str, idempotency_key: str) -> Dict[str, Any]:
        key = _idempotency_key(idempotency_key)
        quest = self.registry.get(quest_id)
        slot = self._slot(quest.quest_id, require_open=True)
        with slot.lock:
            self._ensure_open()
            current = self._public_status_locked(quest, slot)
            if key in slot.start_keys:
                return current
            self._check_key_capacity(slot.start_keys, key)

            try:
                if not current["active"]:
                    # Preserve the fail-before-mutation qualification/owner
                    # boundary for a request that could authorize a Popen.
                    self._assert_local_owner_identity(quest)
                authorization = QuestRuntimeSettings(
                    quest.work_root, quest.quest_id
                ).prepare_explicit_start(key, active=current["active"])
                if (current["active"]
                        or authorization["authorized"] is not True):
                    slot.start_keys.add(key)
                    return current
                runtime_profile, _desired_profile = (
                    self._runtime_profile_launch(quest))
                slot.child = self._spawn_managed_child(
                    quest, start_key=key,
                    runtime_profile=runtime_profile,
                    owner_intent_revision=(
                        authorization["owner_intent_revision"]))
                slot.bound_profile_recovery_attempts = 0
                slot.start_keys.add(key)
                # A durable binding wins over latest settings.  Re-read after
                # Popen so an update committed in the snapshot->spawn window
                # cannot leave an unmonitored, immediately-failing child.
                self._refresh_runtime_restart_after_spawn_locked(
                    quest, slot, start_key=key)
            except QuestProcessUnavailableError:
                raise
            except (OSError, ValueError, RuntimeSettingsCorruptError) as error:
                raise QuestProcessManagerError(
                    "quest run owner 无法启动") from error

            # Popen only proves that the launcher was forked.  Observe a
            # deliberately short window so import/configuration failures are
            # returned to the Web request as ``exited``, while a slower but
            # healthy bootstrap remains honestly ``starting``.  This is not a
            # supervisor readiness protocol; the durable instance lease is the
            # only positive signal used here.
            deadline = time.monotonic() + _START_OBSERVE_TIMEOUT_S
            while True:
                status = self._public_status_locked(quest, slot)
                if status["state"] != "starting":
                    return status
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return status
                time.sleep(min(_START_OBSERVE_POLL_INTERVAL_S, remaining))

    def status(self, quest_id: str) -> Dict[str, Any]:
        quest = self.registry.get(quest_id)
        slot = self._slot(quest.quest_id, require_open=False)
        with slot.lock:
            return self._public_status_locked(quest, slot)

    def _runtime_restart_response_locked(
            self, quest: Quest, slot: _QuestSlot, *, outcome: str,
            current_profile: Dict[str, Any]) -> Dict[str, Any]:
        response = dict(self._public_status_locked(quest, slot))
        response.update({
            "runtime_profile_restart": outcome,
            "runtime_profile_revision": current_profile["revision"],
            "runtime_profile_record_sha256": current_profile["record_sha256"],
        })
        return response

    def _start_runtime_restart_watcher_locked(
            self, quest: Quest, slot: _QuestSlot) -> None:
        watcher = slot.runtime_restart_watcher
        if watcher is not None and watcher.is_alive():
            return
        child = slot.child
        if child is None:
            return
        watcher = threading.Thread(
            target=self._runtime_restart_watcher,
            args=(quest, slot, child), daemon=True,
            name=f"meta-research-runtime-restart-{quest.quest_id}")
        slot.runtime_restart_watcher = watcher
        watcher.start()

    def _runtime_restart_watcher(
            self, quest: Quest, slot: _QuestSlot,
            observed_child: _ManagedChild) -> None:
        """Wait for cooperative exit, then spawn latest; never signal a group."""
        this_thread = threading.current_thread()
        try:
            while True:
                with self._guard:
                    closed = self._closed
                with slot.lock:
                    if (closed or not slot.runtime_restart_requested
                            or slot.child is not observed_child
                            or observed_child.stop_requested
                            or observed_child.terminating):
                        return
                observed_child.process.poll()
                if not self._group_alive(observed_child.process_group_id):
                    try:
                        observed_child.process.wait(timeout=0)
                    except subprocess.TimeoutExpired:
                        pass
                    break
                time.sleep(_RUNTIME_RESTART_POLL_INTERVAL_S)

            with slot.lock:
                with self._guard:
                    if self._closed:
                        return
                if (not slot.runtime_restart_requested
                        or slot.child is not observed_child
                        or observed_child.stop_requested
                        or observed_child.terminating):
                    return
                # Re-read under the spawn decision lock.  Multiple committed
                # updates therefore collapse to the newest strict ledger
                # identity instead of launching every intermediate revision.
                launch_profile, desired_profile = (
                    self._runtime_profile_launch(quest))
                launch_identity = self._validate_runtime_profile_record(
                    quest, launch_profile)
                desired_identity = self._validate_runtime_profile_record(
                    quest, desired_profile)
                observed_identity = (
                    observed_child.applied_runtime_revision,
                    observed_child.applied_runtime_record_sha256)
                if launch_identity == observed_identity == desired_identity:
                    slot.runtime_restart_requested = False
                    slot.runtime_restart_target_revision = None
                    slot.runtime_restart_target_sha256 = None
                    slot.runtime_restart_start_key = None
                    return
                binding_still_requires_old_profile = (
                    launch_identity != desired_identity)
                if binding_still_requires_old_profile:
                    if (slot.bound_profile_recovery_attempts
                            >= _MAX_AUTOMATIC_BOUND_PROFILE_RECOVERIES):
                        slot.runtime_restart_requested = False
                        slot.runtime_restart_error = (
                            "RuntimeBindingRecoveryExhausted")
                        return
                    slot.bound_profile_recovery_attempts += 1
                else:
                    # The recovery owner cleared the binding at a proven
                    # no-inflight boundary.  Latest is now a normal one-shot
                    # replacement, not another old-policy crash recovery.
                    slot.bound_profile_recovery_attempts = 0
                start_key = slot.runtime_restart_start_key or observed_child.start_key
                replacement = self._spawn_managed_child(
                    quest, start_key=start_key,
                    runtime_profile=launch_profile,
                    owner_intent_revision=(
                        observed_child.owner_intent_revision))
                slot.child = replacement
                self._refresh_runtime_restart_after_spawn_locked(
                    quest, slot, start_key=start_key)
        except BaseException as error:
            # The request already returned asynchronously.  Preserve a
            # path-free diagnostic for a later idempotent/retry response, and
            # leave the stopped owner stopped rather than entering a restart
            # storm against a corrupt profile or failed preflight.
            with slot.lock:
                slot.runtime_restart_requested = False
                slot.runtime_restart_error = type(error).__name__
        finally:
            with slot.lock:
                if slot.runtime_restart_watcher is this_thread:
                    slot.runtime_restart_watcher = None
                # An update request can arrive after this watcher installed a
                # replacement but before this thread reaches ``finally``.  It
                # sees the still-live watcher and intentionally does not start
                # a duplicate; hand that pending request to a watcher bound to
                # the new child before retiring.
                with self._guard:
                    closed = self._closed
                if (not closed and slot.runtime_restart_requested
                        and slot.child is not None
                        and slot.child is not observed_child):
                    self._start_runtime_restart_watcher_locked(quest, slot)

    def schedule_runtime_profile_restart(
            self, quest_id: str, idempotency_key: str) -> Dict[str, Any]:
        """Schedule replacement after the current owner reaches a cycle boundary.

        This method deliberately has no signal path.  The owner observes the
        append-only settings ledger, finishes any inflight durable cycle under
        its captured policy, and exits through normal ``System.close``.  Only
        then may this manager reuse its in-memory spawn capability.
        """
        key = _idempotency_key(idempotency_key)
        quest = self.registry.get(quest_id)
        slot = self._slot(quest.quest_id, require_open=True)
        with slot.lock:
            self._ensure_open()
            latest = self._runtime_profile_current(quest)
            current = self._public_status_locked(quest, slot)
            try:
                durable_operation = QuestRuntimeSettings(
                    quest.work_root, quest.quest_id
                ).runtime_update_operation_by_key(key)
            except (OSError, ValueError, RuntimeSettingsCorruptError) as error:
                raise QuestProcessUnavailableError(
                    "runtime profile restart receipt 无法严格核验") from error
            if (durable_operation is not None
                    and durable_operation["status"] in {
                        "applied", "not-required", "terminated"}):
                return self._runtime_restart_response_locked(
                    quest, slot, outcome="not_required",
                    current_profile=latest)
            if current["active"] and not current["managed_by_web"]:
                raise QuestProcessUnavailableError(
                    "external_active owner 不受本 manager signal/spawn capability 管理；"
                    "拒绝安排 runtime profile 重启")
            known_key = key in slot.runtime_restart_keys
            if not known_key:
                self._check_key_capacity(slot.runtime_restart_keys, key)
            if not current["active"]:
                exited_child = slot.child
                stale_managed_generation = (
                    exited_child is not None
                    and not exited_child.stop_requested
                    and (exited_child.applied_runtime_revision,
                         exited_child.applied_runtime_record_sha256)
                    != (latest["revision"], latest["record_sha256"]))
                retry_unfinished_key = (
                    known_key and key in slot.runtime_restart_scheduled_keys)
                durable_accepted = (
                    durable_operation is not None
                    and durable_operation["status"] == "accepted")
                if not (stale_managed_generation or retry_unfinished_key
                        or durable_accepted):
                    # A first request for a genuinely inactive quest only
                    # persists settings; it must not turn "save" into start.
                    slot.runtime_restart_keys.add(key)
                    return self._runtime_restart_response_locked(
                        quest, slot, outcome="not_required",
                        current_profile=latest)
                # Retry either a previously scheduled side effect or a stale
                # Web-managed generation which exited in the update/startup
                # race.  A never-started inactive quest has no child
                # capability and still remains a strict no-op above.
                slot.runtime_restart_keys.add(key)
                slot.runtime_restart_scheduled_keys.add(key)
                launch_profile, _desired_profile = (
                    self._runtime_profile_launch(quest))
                try:
                    if durable_accepted:
                        owner_intent_revision = (
                            durable_operation["owner_intent_revision"])
                    elif exited_child is not None:
                        owner_intent_revision = (
                            exited_child.owner_intent_revision)
                    else:
                        owner_intent_revision = 0
                    replacement = self._spawn_managed_child(
                        quest, start_key=key,
                        runtime_profile=launch_profile,
                        owner_intent_revision=owner_intent_revision)
                except BaseException as error:
                    slot.runtime_restart_error = type(error).__name__
                    raise
                slot.child = replacement
                slot.bound_profile_recovery_attempts = 0
                self._refresh_runtime_restart_after_spawn_locked(
                    quest, slot, start_key=key)
                return self._runtime_restart_response_locked(
                    quest, slot, outcome="scheduled",
                    current_profile=latest)
            child = slot.child
            if (child is None or child.terminating or child.stop_requested
                    or current["state"] == "stopping"):
                raise QuestProcessUnavailableError(
                    "quest owner 正在显式停止，拒绝并发安排 runtime profile 重启")
            latest_identity = (latest["revision"], latest["record_sha256"])
            applied_identity = (
                child.applied_runtime_revision,
                child.applied_runtime_record_sha256)
            slot.runtime_restart_keys.add(key)
            if latest_identity == applied_identity:
                # A repeated key after successful replacement observes the
                # completed side effect.  Do not manufacture a new generation.
                return self._runtime_restart_response_locked(
                    quest, slot, outcome="not_required",
                    current_profile=latest)
            slot.runtime_restart_scheduled_keys.add(key)
            slot.runtime_restart_requested = True
            slot.runtime_restart_target_revision = latest["revision"]
            slot.runtime_restart_target_sha256 = latest["record_sha256"]
            slot.runtime_restart_start_key = key
            slot.runtime_restart_error = None
            self._start_runtime_restart_watcher_locked(quest, slot)
            return self._runtime_restart_response_locked(
                quest, slot, outcome="scheduled",
                current_profile=latest)

    def log_tail(self, quest_id: str) -> Dict[str, Any]:
        """Return a bounded, path-redacted diagnostic for the local Web UI.

        The durable log remains an internal file capability.  Users can see
        why a process exited in the product without being sent to a backend
        work-root or receiving filesystem authority in the response.
        """
        quest = self.registry.get(quest_id)
        path = quest.work_root / _LOG_REF
        if not os.path.lexists(path):
            return {"quest_id": quest.quest_id, "available": False, "text": ""}
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as error:
            raise QuestProcessUnavailableError("Web owner 诊断日志不可读") from error
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size > _MAX_LOG_BYTES_BEFORE_TRUNCATE):
                raise QuestProcessUnavailableError("Web owner 诊断日志身份非法")
            offset = max(0, before.st_size - _MAX_PUBLIC_LOG_TAIL_BYTES)
            raw = os.pread(fd, before.st_size - offset, offset)
            after = os.fstat(fd)
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 after.st_ctime_ns, after.st_mode, after.st_uid, after.st_nlink)
                    != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                        before.st_ctime_ns, before.st_mode, before.st_uid, before.st_nlink)):
                raise QuestProcessUnavailableError("Web owner 诊断日志读取期间漂移")
        finally:
            os.close(fd)
        text = raw.decode("utf-8", errors="replace")
        if offset:
            newline = text.find("\n")
            text = text[newline + 1:] if newline >= 0 else ""
        text = "".join(
            char if char in "\n\t" or ord(char) >= 0x20 else "�"
            for char in text)
        for secret, replacement in (
                (str(quest.work_root), "[quest]"),
                (str(self.system_root), "[system]"),
                (self.connector_profile, "[connector-profile]")):
            if secret:
                text = text.replace(secret, replacement)
        # Tracebacks often contain other absolute implementation paths.  They
        # are not actionable product information, so retain the message while
        # reducing those path-shaped spans to a stable label.
        text = re.sub(r"(?<![A-Za-z0-9_.-])/(?:[^\s:'\"]+/)+[^\s:'\"]*", "[path]", text)
        if len(text) > _MAX_PUBLIC_LOG_CHARS:
            text = text[-_MAX_PUBLIC_LOG_CHARS:]
        return {"quest_id": quest.quest_id, "available": True, "text": text}

    @staticmethod
    def _send_group(child: _ManagedChild, sig: signal.Signals) -> None:
        try:
            os.killpg(child.process_group_id, sig)
        except ProcessLookupError:
            return
        except PermissionError as error:
            raise QuestProcessManagerError(
                "Web owner 已失去对所持进程组的 signal 权限") from error

    def _wait_group_gone(self, child: _ManagedChild, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            child.process.poll()
            if not self._group_alive(child.process_group_id):
                # Reap the leader if it exited between poll and group probing.
                try:
                    child.process.wait(timeout=0)
                except subprocess.TimeoutExpired:
                    pass
                return child.process.poll() is not None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_GROUP_POLL_INTERVAL_S, remaining))

    def _terminate_child(self, child: _ManagedChild) -> None:
        child_code = child.process.poll()
        if child_code is not None and not self._group_alive(child.process_group_id):
            child.terminating = False
            return
        child.terminating = True
        child.stop_requested = True
        self._send_group(child, signal.SIGTERM)
        if self._wait_group_gone(child, _TERMINATE_TIMEOUT_S):
            child.terminating = False
            return
        self._send_group(child, signal.SIGKILL)
        if not self._wait_group_gone(child, _KILL_TIMEOUT_S):
            raise QuestProcessManagerError(
                "Web owner 进程组在 SIGKILL 后仍无法证明消失")
        child.terminating = False

    def terminate(self, quest_id: str, idempotency_key: str) -> Dict[str, Any]:
        key = _idempotency_key(idempotency_key)
        quest = self.registry.get(quest_id)
        slot = self._slot(quest.quest_id, require_open=True)
        with slot.lock:
            self._ensure_open()
            current = self._public_status_locked(quest, slot)
            if key in slot.terminate_keys:
                return current
            self._check_key_capacity(slot.terminate_keys, key)
            try:
                stop_intent = QuestRuntimeSettings(
                    quest.work_root, quest.quest_id
                ).record_explicit_stop(key)
            except (OSError, ValueError, RuntimeSettingsCorruptError) as error:
                raise QuestProcessUnavailableError(
                    "显式停止无法持久化 runtime restart 取消意图") from error
            if stop_intent["applied"] is not True:
                slot.terminate_keys.add(key)
                return current
            # Explicit terminate wins over an asynchronous profile restart.
            # Its existing signal/escalation semantics are unchanged, while
            # the restart watcher is mechanically prevented from respawning.
            slot.runtime_restart_requested = False
            slot.runtime_restart_target_revision = None
            slot.runtime_restart_target_sha256 = None
            slot.runtime_restart_start_key = None
            if current["terminable"]:
                assert slot.child is not None
                self._terminate_child(slot.child)
                current = self._public_status_locked(quest, slot)
            slot.terminate_keys.add(key)
            return current

    def close(self) -> None:
        with self._guard:
            if self._close_complete:
                return
            self._closed = True
            slots = list(self._slots.values())
        errors = []
        restart_watchers = []
        for slot in slots:
            with slot.lock:
                slot.runtime_restart_requested = False
                watcher = slot.runtime_restart_watcher
                if watcher is not None:
                    restart_watchers.append(watcher)
                child = slot.child
                if child is None:
                    continue
                try:
                    self._terminate_child(child)
                except BaseException as error:
                    errors.append(error)
        for watcher in restart_watchers:
            watcher.join(timeout=_RUNTIME_RESTART_JOIN_TIMEOUT_S)
            if watcher.is_alive():
                errors.append(RuntimeError(
                    "runtime profile restart watcher 无法停止"))
        if errors:
            raise QuestProcessManagerError(
                f"quest process manager 关闭失败（{len(errors)} 个 owner）") from errors[0]
        self._launch_queue.put(_LAUNCHER_STOP)
        self._launcher_thread.join(timeout=_LAUNCHER_JOIN_TIMEOUT_S)
        if self._launcher_thread.is_alive():
            raise QuestProcessManagerError("quest owner 常驻启动器无法停止")
        with self._guard:
            self._close_complete = True

    def __enter__(self) -> "QuestProcessManager":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.close()
        except BaseException as close_error:
            if exc is None:
                raise
            add_note = getattr(exc, "add_note", None)
            if callable(add_note):
                add_note(
                    "quest process manager close 失败: "
                    f"{type(close_error).__name__}: {close_error}")


__all__ = [
    "QuestProcessManager", "QuestProcessManagerClosedError",
    "QuestProcessManagerError", "QuestProcessUnavailableError",
]
