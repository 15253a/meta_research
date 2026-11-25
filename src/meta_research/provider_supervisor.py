from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import platform as system_platform
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

from meta_research.linux_provider_tree import LinuxProviderJob, ProviderTreeError


TRANSPORT_KEY_BYTES = 32
SUPERVISOR_REQUEST_SCHEMA = "meta-research/codex-provider-supervisor-request/v1"
CODEX_SUPERVISOR_REQUEST_SCHEMA_V2 = (
    "meta-research/codex-provider-supervisor-request/v2"
)
SUPERVISOR_EXIT_SCHEMA = "meta-research/codex-provider-supervisor-exit/v1"
SUPERVISOR_STARTUP_GRACE_SECONDS = 5.5
PROVIDER_OPERATION_ENV = "META_RESEARCH_PROVIDER_OPERATION"
SUPERVISOR_STOP_SCHEMA = "meta-research/codex-provider-supervisor-stop/v1"
SUPERVISOR_REQUEST_SCHEMA_V2 = "meta-research/provider-supervisor-request/v2"
SUPERVISOR_EXIT_SCHEMA_V2 = "meta-research/provider-supervisor-exit/v2"
SUPERVISOR_HEARTBEAT_SCHEMA = "meta-research/provider-supervisor-heartbeat/v1"
SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS = 15.0
SUPERVISOR_HEARTBEAT_STALE_SECONDS = 45.0
SUPERVISOR_HEARTBEAT_MAX_FUTURE_SKEW_SECONDS = 60.0
SUPERVISOR_HEARTBEAT_REPLACE_RETRY_SECONDS = 1.0
_OPERATION_SCOPE_FIELDS = frozenset({
    "operation_name", "root_kind", "job_ref", "run_ref", "attempt_ref",
    "fence_ref", "operation_ref", "model_ref",
    "workspace_ref", "root_session_ref", "work_ref", "quest_ref",
    "cycle_ref", "request_ref", "target_ref",
})
_SUPERVISOR_REQUEST_SCHEMAS = {
    SUPERVISOR_REQUEST_SCHEMA,
    CODEX_SUPERVISOR_REQUEST_SCHEMA_V2,
    SUPERVISOR_REQUEST_SCHEMA_V2,
}
_SUPERVISOR_EXIT_SCHEMAS = {
    SUPERVISOR_EXIT_SCHEMA,
    SUPERVISOR_EXIT_SCHEMA_V2,
}
PROVIDER_SUPERVISOR_MAX_CONTENT_BYTES = 64 * 1024 * 1024
# Root Target turns may legitimately own provider work for days.  Keep a
# finite, operator-configurable safety ceiling without forcing them through
# the ordinary short interactive watchdog.
PROVIDER_SUPERVISOR_MAX_TIMEOUT_SECONDS = 365 * 24 * 60 * 60


class ProviderSupervisorError(RuntimeError):
    pass


LockOperation = Callable[[int, bool, bool], None]


class SupervisorFileLock:
    """Cross-platform lock for one durable provider operation."""

    def __init__(
        self,
        path: Path,
        *,
        platform_name: str | None = None,
        lock_operation: LockOperation | None = None,
    ) -> None:
        self._path = path
        self._platform_name = platform_name or os.name
        self._lock_operation = lock_operation
        self._handle: BinaryIO | None = None

    def acquire(self, *, blocking: bool = True) -> bool:
        if self._handle is not None:
            raise RuntimeError("provider supervisor lock is already acquired")
        operation = self._lock_operation or _platform_lock_operation(
            self._platform_name
        )
        handle = self._path.open("a+b")
        if self._platform_name == "nt":
            try:
                _ensure_windows_lock_byte(handle)
            except Exception:
                handle.close()
                raise
        while True:
            try:
                operation(handle.fileno(), True, blocking)
            except OSError as error:
                if error.errno not in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EDEADLK,
                }:
                    handle.close()
                    raise
                if blocking:
                    time.sleep(0.05)
                    continue
                handle.close()
                return False
            except Exception:
                handle.close()
                raise
            self._handle = handle
            return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            operation = self._lock_operation or _platform_lock_operation(
                self._platform_name
            )
            operation(handle.fileno(), False, False)
        finally:
            handle.close()

    def __enter__(self) -> "SupervisorFileLock":
        if not self.acquire():
            raise RuntimeError("provider supervisor lock could not be acquired")
        return self

    def __exit__(self, *_error: object) -> None:
        self.release()


def _platform_lock_operation(platform_name: str) -> LockOperation:
    if platform_name == "posix":
        return _posix_lock_operation
    if platform_name == "nt":
        return _windows_lock_operation
    raise ProviderSupervisorError("provider_supervisor_platform_unsupported")


def _posix_lock_operation(
    descriptor: int, acquire: bool, blocking: bool
) -> None:
    import fcntl

    operation = fcntl.LOCK_EX if acquire else fcntl.LOCK_UN
    if acquire and not blocking:
        operation |= fcntl.LOCK_NB
    fcntl.flock(descriptor, operation)


def _windows_lock_operation(
    descriptor: int, acquire: bool, _blocking: bool
) -> None:
    import msvcrt

    os.lseek(descriptor, 0, os.SEEK_SET)
    operation = msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK
    msvcrt.locking(descriptor, operation, 1)


def _ensure_windows_lock_byte(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0, os.SEEK_SET)


class ProviderProcessJob(Protocol):
    """One provider process tree with an exact, queryable lifetime."""

    def spawn(
        self,
        argv: list[str],
        **options: object,
    ) -> subprocess.Popen[bytes]: ...

    def active_process_count(self) -> int: ...

    def terminate(self, exit_code: int = 1) -> bool: ...

    def close(self) -> None: ...


ProviderProcessJobFactory = Callable[[], ProviderProcessJob]


class WindowsProviderJob:
    """Windows Job Object that owns the complete provider process tree."""

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _CREATE_SUSPENDED = 0x00000004

    def __init__(self) -> None:
        if os.name != "nt":
            raise ProviderSupervisorError("provider_windows_job_unavailable")
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = wintypes.LONG

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            raise error
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = kernel32
        self._ntdll = ntdll
        self._accounting_type = BasicAccountingInformation
        self._handle = handle

    def spawn(
        self,
        argv: list[str],
        **options: object,
    ) -> subprocess.Popen[bytes]:
        if self._handle is None:
            raise ProviderSupervisorError("provider_windows_job_closed")
        popen_options = dict(options)
        creationflags = int(popen_options.pop("creationflags", 0))
        process = subprocess.Popen(
            argv,
            creationflags=creationflags | self._CREATE_SUSPENDED,
            **cast(dict[str, Any], popen_options),
        )
        process_handle = self._wintypes.HANDLE(int(process._handle))
        try:
            if not self._kernel32.AssignProcessToJobObject(
                self._handle,
                process_handle,
            ):
                raise self._ctypes.WinError(self._ctypes.get_last_error())
            if self._ntdll.NtResumeProcess(process_handle) != 0:
                raise ProviderSupervisorError("provider_windows_resume_failed")
        except BaseException:
            self._kernel32.TerminateProcess(process_handle, 1)
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired as error:
                raise ProviderSupervisorError(
                    "provider_process_termination_unconfirmed"
                ) from error
            raise
        return process

    def active_process_count(self) -> int:
        if self._handle is None:
            raise ProviderSupervisorError("provider_windows_job_closed")
        information = self._accounting_type()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            self._ctypes.byref(information),
            self._ctypes.sizeof(information),
            None,
        ):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        return int(information.ActiveProcesses)

    def terminate(self, exit_code: int = 1) -> bool:
        if self._handle is None:
            return False
        if not self._kernel32.TerminateJobObject(self._handle, exit_code):
            return False
        deadline = time.monotonic() + 1.0
        while self.active_process_count() > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.active_process_count() == 0

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        if not self._kernel32.CloseHandle(handle):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        self._handle = None


class ProviderProcessPlatform:
    """Own platform-specific provider process identity and control."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        windows_pid_probe: Callable[[int], bool] | None = None,
        create_new_process_group: int | None = None,
        detached_process: int | None = None,
    ) -> None:
        self.platform_name = platform_name or os.name
        self._windows_pid_probe = windows_pid_probe or _windows_pid_is_alive
        self._create_new_process_group = (
            create_new_process_group
            if create_new_process_group is not None
            else getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        self._detached_process = (
            detached_process
            if detached_process is not None
            else getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )

    def provider_spawn_options(self) -> dict[str, object]:
        if self.platform_name == "posix":
            return {"start_new_session": True}
        if self.platform_name == "nt":
            return {"creationflags": self._create_new_process_group}
        raise ProviderSupervisorError("provider_supervisor_platform_unsupported")

    def supervisor_spawn_options(self) -> dict[str, object]:
        if self.platform_name == "posix":
            return {"close_fds": True, "start_new_session": True}
        if self.platform_name == "nt":
            return {
                "close_fds": True,
                "creationflags": (
                    self._create_new_process_group | self._detached_process
                ),
            }
        raise ProviderSupervisorError("provider_supervisor_platform_unsupported")

    def current_process_group(self) -> int:
        if self.platform_name == "posix":
            return os.getpgrp()
        if self.platform_name == "nt":
            return os.getpid()
        raise ProviderSupervisorError("provider_supervisor_platform_unsupported")

    def process_group_for_pid(self, process_id: int) -> int:
        if self.platform_name == "posix":
            return os.getpgid(process_id)
        if self.platform_name == "nt":
            return process_id
        raise ProviderSupervisorError("provider_supervisor_platform_unsupported")

    def process_group_running(self, process_group: int) -> bool:
        if process_group <= 1:
            return False
        if self.platform_name == "nt":
            return self._windows_pid_probe(process_group)
        if self.platform_name != "posix":
            return False
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def terminate_process_group(self, process_group: int) -> bool:
        if process_group <= 1:
            return False
        if self.platform_name == "nt":
            raise ProviderSupervisorError("provider_windows_job_required")
        if self.platform_name != "posix":
            return False
        return _terminate_posix_process_group(process_group)


def provider_spawn_options() -> dict[str, object]:
    return ProviderProcessPlatform().provider_spawn_options()


def supervisor_spawn_options() -> dict[str, object]:
    return ProviderProcessPlatform().supervisor_spawn_options()




def protected_subprocess_environment(
    *,
    protected: Mapping[str, str],
    requested: Mapping[str, str] | None = None,
    source_environment: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> dict[str, str]:
    """Merge an invocation environment without allowing protected overrides.

    Windows treats environment keys case-insensitively.  Remove every spelling
    of a protected key before installing the owner-controlled spelling so a
    caller cannot escape a storage boundary with (for example) ``codex_home``.
    """

    selected_platform = platform_name or os.name
    source = os.environ if source_environment is None else source_environment
    environment = dict(source)
    if requested is not None:
        environment.update(requested)
    if selected_platform == "nt":
        protected_by_name = {
            name.upper(): (name, value) for name, value in protected.items()
        }
        environment = {
            name: value
            for name, value in environment.items()
            if name.upper() not in protected_by_name
        }
        environment.update(dict(protected_by_name.values()))
    else:
        environment.update(protected)
    return environment


def ensure_provider_account_current(
    argv: list[str], environment: Mapping[str, str] | None,
) -> None:
    """Fence new Codex calls, while keeping local admission probes available."""
    arguments = argv[1:]
    while arguments and arguments[0] in {"--config", "-c", "--enable", "--disable"}:
        arguments = arguments[2:]
    native = bool(arguments and arguments[0] == "exec")
    if arguments and arguments[0] == "app-server":
        native = not any(value in {"generate-json-schema", "generate-ts"} for value in arguments[1:])
    if arguments[:2] == ["-m", "meta_research.native_codex_bridge"]:
        native = True
    if arguments[:2] == ["-m", "meta_research.harness_cli_bridge"]:
        native = any(arguments[index:index + 2] == ["--family", "codex"]
                     for index in range(2, len(arguments) - 1))
    home = (os.environ if environment is None else environment).get("CODEX_HOME")
    if not native or not home:
        return
    from meta_research.codex_account import CodexAccountStateError, ensure_codex_account_current
    try:
        ensure_codex_account_current(Path(home))
    except CodexAccountStateError as error:
        raise OSError(error.code) from error


def current_process_group() -> int:
    return ProviderProcessPlatform().current_process_group()


def process_group_for_pid(process_id: int) -> int:
    return ProviderProcessPlatform().process_group_for_pid(process_id)


def _windows_pid_is_alive(process_id: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        return ctypes.get_last_error() == access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


@dataclass(frozen=True)
class TypedExecutionFence:
    """Cross-provider Run/Attempt/Session/Fence identity invariant."""

    run_ref: str
    attempt_ref: str
    generation: int
    root_session_ref: str
    fence_ref: str

    def validate(self) -> None:
        if (
            not self.run_ref
            or not self.attempt_ref
            or isinstance(self.generation, bool)
            or self.generation < 1
            or not self.root_session_ref
            or not self.fence_ref
        ):
            raise ProviderSupervisorError("typed_execution_fence_invalid")


def provider_operation_ref(
    run_ref: str,
    operation_kind: str,
    generation: int,
) -> str:
    """Stable effect identity shared by durable provider Run implementations."""

    if (
        not run_ref
        or not operation_kind
        or ":" in operation_kind
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise ProviderSupervisorError("provider_operation_identity_invalid")
    value = f"{run_ref}:{operation_kind}:{generation}"
    if len(value) > 128:
        raise ProviderSupervisorError("provider_operation_identity_invalid")
    return value


def ensure_transport_key(workspace: Path) -> tuple[Path, bytes]:
    operation_root = workspace / "provider-operations"
    operation_root.mkdir(parents=True, exist_ok=True)
    key_path = operation_root / ".transport-seal.key"
    if not key_path.exists():
        _publish_exclusive(
            key_path,
            secrets.token_bytes(TRANSPORT_KEY_BYTES),
        )
    return key_path, _read_transport_key(key_path)


def transport_key_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def transport_canonical_json(value: object) -> str:
    """Canonical transport encoding shared by durable provider adapters."""

    return _canonical_json(value)


def sealed_transport_envelope(
    payload: dict[str, object], key: bytes
) -> dict[str, object]:
    return {"payload": payload, "seal": _seal(payload, key)}


def verify_transport_envelope(
    envelope: dict[str, object], key: bytes
) -> dict[str, object]:
    return _verified_envelope_payload(envelope, key)


def write_transport_envelope(
    path: Path, payload: dict[str, object], key: bytes
) -> None:
    _write_signed_envelope(path, payload, key)


def read_transport_envelope(path: Path, key: bytes) -> dict[str, object]:
    return _read_signed_envelope(path, key)


def transport_file_sha256(path: Path) -> str:
    return _file_sha256(path)


def provider_process_group_running(process_group: int) -> bool:
    return ProviderProcessPlatform().process_group_running(process_group)


def terminate_provider_process_group(process_group: int) -> bool:
    return ProviderProcessPlatform().terminate_process_group(process_group)


def request_supervisor_stop(
    operation_directory: Path,
    *,
    key: bytes,
    invocation_hash: str,
    ready_schema: str,
    wait_seconds: float = 2.0,
    process_platform: ProviderProcessPlatform | None = None,
) -> bool:
    """Request a verified detached supervisor to reach a signed terminal boundary.

    The ready marker is sealed by the operation transport key and binds the PID to
    the exact invocation. POSIX verifies the live command line before signalling;
    Windows uses only the sealed per-operation stop channel and never signals a
    possibly reused PID from the marker.
    """

    receipt_path = operation_directory / "supervisor-exit.json"
    def terminal_receipt() -> bool:
        if not receipt_path.is_file():
            return False
        read_verified_exit_receipt(
            receipt_path, key=key, invocation_hash=invocation_hash,
            prompt_path=operation_directory / "prompt.txt",
            schema_path=operation_directory / "output-schema.json",
            stdout_path=operation_directory / "stdout.jsonl",
            result_path=operation_directory / "last-message.json",
            expected_schema_ref=(SUPERVISOR_EXIT_SCHEMA_V2
                if ready_schema == "meta-research/provider-supervisor-ready/v2"
                else SUPERVISOR_EXIT_SCHEMA),
        )
        return True
    if receipt_path.is_file():
        return terminal_receipt()
    ready = read_transport_envelope(
        operation_directory / "supervisor-ready.json", key
    )
    process_id = ready.get("supervisor_process_id")
    process_group = ready.get("supervisor_process_group")
    ready_keys = {
        "schema_ref",
        "invocation_hash",
        "supervisor_process_id",
        "supervisor_process_group",
    }
    if (
        frozenset(ready)
        not in {frozenset(ready_keys), frozenset({*ready_keys, "phase"}),
                frozenset({*ready_keys, "boot_identity_hash", "host_identity_hash"})}
        or ready.get("schema_ref") != ready_schema
        or ("phase" in ready and ready.get("phase") != "ready")
        or ready.get("invocation_hash") != invocation_hash
        or not isinstance(process_id, int)
        or isinstance(process_id, bool)
        or process_id <= 1
        or not isinstance(process_group, int)
        or isinstance(process_group, bool)
        or process_group != process_id
    ):
        raise ProviderSupervisorError("provider_supervisor_ready_invalid")
    platform = process_platform or ProviderProcessPlatform()
    request_path = (operation_directory / "supervisor-request.json").resolve()
    if platform.platform_name == "posix":
        try:
            live_group = platform.process_group_for_pid(process_id)
            command_line = Path(f"/proc/{process_id}/cmdline").read_bytes().split(
                b"\0"
            )
        except ProcessLookupError:
            live_group = None
            command_line = []
        except OSError as error:
            raise ProviderSupervisorError(
                "provider_supervisor_identity_invalid"
            ) from error
        if live_group is not None:
            if (
                live_group != process_group
                or str(request_path).encode() not in command_line
            ):
                raise ProviderSupervisorError(
                    "provider_supervisor_identity_invalid"
                )
            write_supervisor_stop_request(
                operation_directory / "supervisor-stop.json",
                key=key,
                invocation_hash=invocation_hash,
            )
            try:
                os.kill(process_id, signal.SIGTERM)
            except ProcessLookupError:
                pass
    elif platform.platform_name == "nt":
        # The exact sealed operation file is the Windows control channel. Do
        # not signal a possibly reused PID merely because its numeric value is
        # present in an old marker.
        write_supervisor_stop_request(
            operation_directory / "supervisor-stop.json",
            key=key,
            invocation_hash=invocation_hash,
        )
    else:
        raise ProviderSupervisorError("provider_supervisor_platform_unsupported")
    deadline = time.monotonic() + wait_seconds
    while not receipt_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    return terminal_receipt()


def supervisor_request_never_started(
    operation_directory: Path,
    *,
    key: bytes,
    invocation_hash: str,
    request_schema: str,
    platform_name: str | None = None,
) -> bool:
    """Fence an unissued request under the same lock used by its supervisor."""

    request_path = operation_directory / "supervisor-request.json"
    if not request_path.is_file():
        return False
    request = read_transport_envelope(request_path, key)
    if (
        request.get("schema_ref") != request_schema
        or request.get("invocation_hash") != invocation_hash
    ):
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    lock = SupervisorFileLock(operation_directory / "supervisor.lock",
                              platform_name=platform_name)
    if not lock.acquire(blocking=False):
        return False
    try:
        if any((operation_directory / name).exists() for name in (
            "supervisor-ready.json", "provider-started.json", "supervisor-heartbeat.json",
            "provider-stderr.log", "supervisor-exit.json",
        )):
            return False
        write_supervisor_stop_request(operation_directory / "supervisor-stop.json",
                                      key=key, invocation_hash=invocation_hash)
        return True
    finally:
        lock.release()


def read_transport_key_for_operation(
    operation_directory: Path,
) -> tuple[Path, bytes]:
    key_path = _operation_key_path(operation_directory)
    return key_path, _read_transport_key(key_path)


def read_transport_key_for_workspace(workspace: Path) -> bytes:
    """Read the shared provider-operation seal key without creating it."""

    return _read_transport_key(workspace / "provider-operations" / ".transport-seal.key")


def write_supervisor_request(
    path: Path,
    payload: dict[str, object],
    key: bytes,
) -> None:
    if payload.get("schema_ref") not in _SUPERVISOR_REQUEST_SCHEMAS:
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    _write_signed_envelope(path, payload, key)


def read_supervisor_request(path: Path, key: bytes) -> dict[str, object]:
    payload = _read_signed_envelope(path, key)
    if payload.get("schema_ref") not in _SUPERVISOR_REQUEST_SCHEMAS:
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    return payload


def write_supervisor_stop_request(
    path: Path, *, key: bytes, invocation_hash: str
) -> None:
    if not isinstance(invocation_hash, str) or len(invocation_hash) != 64:
        raise ProviderSupervisorError("provider_supervisor_stop_invalid")
    _write_signed_envelope(
        path,
        {
            "schema_ref": SUPERVISOR_STOP_SCHEMA,
            "invocation_hash": invocation_hash,
        },
        key,
    )


def supervisor_stop_requested(
    path: Path, *, key: bytes, invocation_hash: str
) -> bool:
    if not path.exists():
        return False
    payload = _read_signed_envelope(path, key)
    if payload != {
        "schema_ref": SUPERVISOR_STOP_SCHEMA,
        "invocation_hash": invocation_hash,
    }:
        raise ProviderSupervisorError("provider_supervisor_stop_invalid")
    return True


_LINUX_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_LINUX_MACHINE_ID_PATHS = (
    Path("/etc/machine-id"),
    Path("/var/lib/dbus/machine-id"),
    Path("/run/machine-id"),
)
_LINUX_MACHINE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_SAFE_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _current_platform() -> str:
    return sys.platform


def _read_linux_machine_id(
    paths: tuple[Path, ...] = _LINUX_MACHINE_ID_PATHS,
) -> str:
    for candidate_path in paths:
        try:
            value = candidate_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if _LINUX_MACHINE_ID_RE.fullmatch(value):
            return value
    raise ProviderSupervisorError("provider_host_identity_unavailable")


def supervisor_host_identity() -> dict[str, str | None]:
    if _current_platform() == "linux":
        try:
            boot = _LINUX_BOOT_ID_PATH.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise ProviderSupervisorError("provider_host_identity_unavailable") from error
        machine = _read_linux_machine_id(_LINUX_MACHINE_ID_PATHS)
        if not boot:
            raise ProviderSupervisorError("provider_host_identity_unavailable")
        return {
            "boot_identity_hash": hashlib.sha256(
                _canonical_json({"boot_identity": boot}).encode()
            ).hexdigest(),
            "host_identity_hash": hashlib.sha256(
                _canonical_json({"machine_id": machine, "node": system_platform.node()}).encode()
            ).hexdigest(),
        }
    return {
        "boot_identity_hash": None,
        "host_identity_hash": hashlib.sha256(system_platform.node().encode()).hexdigest(),
    }


def emit_supervisor_error(error: BaseException) -> None:
    """Emit a path-free, secret-free structured error code on stderr before exit 70."""
    if isinstance(error, (ProviderSupervisorError, ProviderTreeError)):
        raw = str(error).strip()
        error_code = raw if _SAFE_ERROR_CODE_RE.fullmatch(raw) else type(error).__name__
    elif isinstance(error, OSError) and error.errno is not None:
        error_code = errno.errorcode.get(error.errno, f"errno_{error.errno}")
    else:
        error_code = type(error).__name__
    payload = {
        "schema_ref": "meta-research/provider-supervisor-error/v1",
        "exit_code": 70,
        "error_type": type(error).__name__,
        "error_code": error_code,
    }
    try:
        sys.stderr.write(_canonical_json(payload) + "\n")
        sys.stderr.flush()
    except OSError:
        pass


def verify_process_tree_closed(payload: object) -> None:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_ref", "state", "mechanism", "boot_identity_hash", "host_identity_hash",
    } or payload.get("schema_ref") != "meta-research/provider-process-tree/v1" or (
        payload.get("state") != "closed"
    ) or payload.get("mechanism") not in {
        "linux-subreaper-echild", "windows-job", "not-started", "host-reboot",
    }:
        raise ProviderSupervisorError("provider_process_tree_proof_invalid")
    for name in ("boot_identity_hash", "host_identity_hash"):
        value = payload[name]
        if name == "boot_identity_hash" and value is None and payload["mechanism"] in {
            "windows-job", "not-started",
        }:
            continue
        if not isinstance(value, str) or len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ProviderSupervisorError("provider_process_tree_proof_invalid")


def write_exit_receipt(
    path: Path,
    *,
    key: bytes,
    invocation_hash: str,
    prompt_path: Path,
    schema_path: Path,
    stdout_path: Path,
    result_path: Path,
    stderr_path: Path | None = None,
    returncode: int | None,
    input_bytes: int,
    process_tree: dict[str, object],
    termination_reason: str = "completed",
    schema_ref: str = SUPERVISOR_EXIT_SCHEMA,
    codex_home: Path | None = None,
    stderr_limited: bool | None = None,
    operation_scope: dict[str, object] | None = None,
) -> None:
    if schema_ref not in _SUPERVISOR_EXIT_SCHEMAS:
        raise ProviderSupervisorError("provider_supervisor_exit_invalid")
    verify_process_tree_closed(process_tree)
    if process_tree["mechanism"] == "host-reboot":
        raise ProviderSupervisorError("provider_supervisor_exit_invalid")
    prompt_bytes = prompt_path.stat().st_size
    payload: dict[str, object] = {
        "schema_ref": schema_ref,
        "invocation_hash": invocation_hash,
        "returncode": returncode,
        "termination_reason": termination_reason,
        "process_tree": process_tree,
        "prompt_file_hash": _file_sha256(prompt_path),
        "prompt_bytes": prompt_bytes,
        "input_bytes": input_bytes,
        "input_complete": input_bytes == prompt_bytes,
        "output_schema_file_hash": _file_sha256(schema_path),
        "stdout_file_hash": _file_sha256(stdout_path),
        "result_file_hash": (
            _file_sha256(result_path) if result_path.is_file() else None
        ),
    }

    # Publish call-observation and root-session observation index (or gap) BEFORE
    # revealing supervisor-exit.json so exit never races ahead of usage metadata.
    if stderr_path is None:
        stderr_path = stdout_path.with_name("provider-stderr.log")
    observation_path = path.with_name("call-observation.json")
    from meta_research.provider_call_observations import observe_provider_files
    from meta_research.root_session_usage import (
        RootSessionUsageError,
        _workspace_for_operation,
        write_root_session_observation_gap,
        write_root_session_observation_index,
    )

    try:

        obs_payload: dict[str, object] | None = None
        if observation_path.is_file():
            candidate_obs = _read_signed_envelope(observation_path, key)
            if candidate_obs.get("invocation_hash") != invocation_hash:
                raise ProviderSupervisorError("provider_supervisor_spool_invalid")
            if candidate_obs.get("status") == "unavailable":
                try:
                    write_root_session_observation_gap(
                        path.parent,
                        key=key,
                        invocation_hash=invocation_hash,
                        operation_scope=operation_scope,
                        failure_code=str(
                            candidate_obs.get("failure_code")
                            or "provider_observation_failed"
                        ),
                    )
                except (OSError, ProviderSupervisorError, ValueError, TypeError):
                    pass
                _write_signed_envelope(path, payload, key)
                return
            obs_payload = candidate_obs

        if obs_payload is None:
            rollout_cursor_dir: Path | None = None
            try:
                workspace = _workspace_for_operation(path.parent)
            except (RootSessionUsageError, OSError):
                workspace = None
            else:
                rollout_cursor_dir = workspace / "provider-observations" / "rollout-cursors"

            observation = observe_provider_files(
                prompt_path,
                stdout_path,
                stderr_path=stderr_path,
                stderr_limited=stderr_limited,
                codex_home=codex_home,
                operation_scope=operation_scope,
                rollout_cursor_dir=rollout_cursor_dir,
            )
            obs_payload = {
                **observation,
                "invocation_hash": invocation_hash,
                "output_schema_utf8_bytes": schema_path.stat().st_size,
                "stdout_file_hash": payload["stdout_file_hash"],
                "prompt_file_hash": payload["prompt_file_hash"],
            }
            _write_signed_envelope(observation_path, obs_payload, key)

        try:
            write_root_session_observation_index(
                path.parent,
                key=key,
                invocation_hash=invocation_hash,
                observation=obs_payload,
                operation_scope=operation_scope,
            )
        except Exception as error:
            try:
                write_root_session_observation_gap(
                    path.parent,
                    key=key,
                    invocation_hash=invocation_hash,
                    operation_scope=operation_scope,
                    failure_code=type(error).__name__ or "root_session_observation_failed",
                )
            except (OSError, ProviderSupervisorError, ValueError, TypeError):
                pass
    except (OSError, ValueError, UnicodeError, ProviderSupervisorError) as error:
        failure_code = {
            OSError: "provider_observation_io_error",
            ValueError: "provider_observation_value_error",
            UnicodeError: "provider_observation_encoding_error",
            ProviderSupervisorError: "provider_observation_seal_error",
        }.get(type(error), "provider_observation_failed")
        try:
            _write_signed_envelope(path.with_name("call-observation.json"), {
                "schema_ref": "meta-research/provider-call-observation-unavailable/v1",
                "invocation_hash": invocation_hash,
                "status": "unavailable",
                "failure_code": failure_code,
                "failure_stage": "observe_provider_files",
                "owner": "harness",
                "next_action": "preserve_exit_receipt_and_reconcile_observation_before_using_usage_evidence",
            }, key)
        except (OSError, ProviderSupervisorError, ValueError, TypeError):
            pass
        try:
            write_root_session_observation_gap(
                path.parent,
                key=key,
                invocation_hash=invocation_hash,
                operation_scope=operation_scope,
                failure_code=failure_code,
            )
        except (OSError, ProviderSupervisorError, ValueError, TypeError):
            pass

    _write_signed_envelope(path, payload, key)


def read_verified_exit_receipt(
    path: Path,
    *,
    key: bytes,
    invocation_hash: str,
    prompt_path: Path,
    schema_path: Path,
    stdout_path: Path,
    result_path: Path,
    expected_schema_ref: str = SUPERVISOR_EXIT_SCHEMA,
) -> tuple[dict[str, object], dict[str, object]]:
    if expected_schema_ref not in _SUPERVISOR_EXIT_SCHEMAS:
        raise ProviderSupervisorError("provider_supervisor_exit_invalid")
    envelope = _read_envelope(path)
    payload = _verified_envelope_payload(envelope, key)
    expected_keys = {
        "schema_ref",
        "invocation_hash",
        "returncode",
        "termination_reason",
        "prompt_file_hash",
        "prompt_bytes",
        "input_bytes",
        "input_complete",
        "output_schema_file_hash",
        "stdout_file_hash",
        "result_file_hash",
        "process_tree",
    }
    verify_process_tree_closed(payload.get("process_tree"))
    returncode = payload.get("returncode")
    termination_reason = payload.get("termination_reason")
    prompt_bytes = payload.get("prompt_bytes")
    input_bytes = payload.get("input_bytes")
    current_result_hash = (
        _file_sha256(result_path) if result_path.is_file() else None
    )
    if (
        set(payload) != expected_keys
        or payload.get("schema_ref") != expected_schema_ref
        or payload.get("invocation_hash") != invocation_hash
        or (type(returncode) is not int and not (
            returncode is None and payload["process_tree"]["mechanism"] == "not-started"
            and termination_reason in {"stopped", "launch_failed"}
        ))
        or payload["process_tree"]["mechanism"] == "host-reboot"
        or termination_reason
        not in {
            "completed",
            "timeout",
            "stopped",
            "output_limit",
            "descendant_process",
            "launch_failed",
        }
        or not isinstance(prompt_bytes, int)
        or isinstance(prompt_bytes, bool)
        or not isinstance(input_bytes, int)
        or isinstance(input_bytes, bool)
        or prompt_bytes != prompt_path.stat().st_size
        or (
            termination_reason == "completed"
            and returncode == 0
            and (
                input_bytes != prompt_bytes
                or payload.get("input_complete") is not True
            )
        )
        or payload.get("prompt_file_hash") != _file_sha256(prompt_path)
        or payload.get("output_schema_file_hash") != _file_sha256(schema_path)
        or payload.get("stdout_file_hash") != _file_sha256(stdout_path)
        or payload.get("result_file_hash") != current_result_hash
    ):
        raise ProviderSupervisorError("provider_supervisor_exit_invalid")
    return cast(dict[str, object], payload), envelope


def _operation_key_path(operation_directory: Path) -> Path:
    try:
        operation_root = operation_directory.parents[1]
    except IndexError as error:
        raise ProviderSupervisorError("provider_supervisor_path_invalid") from error
    return operation_root / ".transport-seal.key"


def _read_transport_key(path: Path) -> bytes:
    try:
        value = path.read_bytes()
    except OSError as error:
        raise ProviderSupervisorError("provider_transport_key_unavailable") from error
    if len(value) != TRANSPORT_KEY_BYTES:
        raise ProviderSupervisorError("provider_transport_key_invalid")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _seal(payload: dict[str, object], key: bytes) -> str:
    return hmac.new(
        key,
        _canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _write_signed_envelope(
    path: Path,
    payload: dict[str, object],
    key: bytes,
) -> None:
    envelope = {"payload": payload, "seal": _seal(payload, key)}
    encoded = _canonical_json(envelope)
    if not _publish_exclusive(path, encoded.encode("utf-8")):
        try:
            persisted = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ProviderSupervisorError("provider_supervisor_spool_invalid") from error
        if persisted != encoded:
            raise ProviderSupervisorError("provider_supervisor_spool_invalid")


def _read_signed_envelope(path: Path, key: bytes) -> dict[str, object]:
    return _verified_envelope_payload(_read_envelope(path), key)


def _read_envelope(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderSupervisorError("provider_supervisor_spool_invalid") from error
    if not isinstance(value, dict):
        raise ProviderSupervisorError("provider_supervisor_spool_invalid")
    return cast(dict[str, object], value)


def _verified_envelope_payload(
    envelope: dict[str, object], key: bytes
) -> dict[str, object]:
    payload = envelope.get("payload")
    seal = envelope.get("seal")
    if (
        set(envelope) != {"payload", "seal"}
        or not isinstance(payload, dict)
        or not isinstance(seal, str)
        or not hmac.compare_digest(seal, _seal(payload, key))
    ):
        raise ProviderSupervisorError("provider_supervisor_seal_invalid")
    return cast(dict[str, object], payload)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ProviderSupervisorError("provider_supervisor_spool_invalid") from error
    return digest.hexdigest()


def _publish_exclusive(path: Path, value: bytes) -> bool:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        with temporary.open("xb") as destination:
            _make_private(temporary, destination.fileno())
            destination.write(value)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        _fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _write_signed_replacement(
    path: Path,
    payload: dict[str, object],
    key: bytes,
) -> None:
    """Atomically replace one signed observer snapshot."""
    envelope = {"payload": payload, "seal": _seal(payload, key)}
    encoded = _canonical_json(envelope).encode("utf-8")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        with temporary.open("xb") as destination:
            _make_private(temporary, destination.fileno())
            destination.write(encoded)
            destination.flush()
            os.fsync(destination.fileno())
        deadline = time.monotonic() + SUPERVISOR_HEARTBEAT_REPLACE_RETRY_SECONDS
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows readers may hold the old snapshot without delete
                # sharing. Retry the short observation race, then surface the
                # gap to the existing heartbeat caller.
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_supervisor_heartbeat(
    path: Path,
    *,
    invocation_hash: str,
    key: bytes,
    phase: str,
    process_platform: ProviderProcessPlatform,
    provider_process: subprocess.Popen[bytes] | None = None,
    operation_path: Path | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> None:
    if phase not in {"ready", "running"}:
        raise ProviderSupervisorError("provider_supervisor_heartbeat_invalid")
    payload: dict[str, object] = {
        "schema_ref": SUPERVISOR_HEARTBEAT_SCHEMA,
        "invocation_hash": invocation_hash,
        "phase": phase,
        "observed_at": time.time(),
        "supervisor_process_id": os.getpid(),
        "supervisor_process_group": process_platform.current_process_group(),
        **supervisor_host_identity(),
    }
    if provider_process is not None:
        if operation_path is None:
            raise ProviderSupervisorError("provider_process_identity_invalid")
        payload.update(
            {
                "provider_process_id": provider_process.pid,
                "provider_process_group": process_platform.process_group_for_pid(
                    provider_process.pid
                ),
                "provider_operation_path": str(operation_path.resolve()),
            }
        )
    if stdout_path is not None:
        try:
            payload["stdout_bytes"] = stdout_path.stat().st_size
        except OSError:
            # The stream may not be visible during an observer refresh. Omit
            # this optional field rather than publishing an invalid marker.
            pass
    if stderr_path is not None:
        try:
            payload["stderr_bytes"] = stderr_path.stat().st_size
        except OSError:
            pass
    _write_signed_replacement(path, payload, key)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # The file itself has been flushed. Python cannot portably open and
        # fsync a Windows directory handle.
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validated_working_directory(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ProviderSupervisorError("provider_working_directory_invalid")
    path = Path(value)
    try:
        if not path.is_absolute() or not path.is_dir() or path.resolve(strict=True) != path:
            raise ProviderSupervisorError("provider_working_directory_invalid")
    except OSError as error:
        raise ProviderSupervisorError("provider_working_directory_invalid") from error
    return path


def _validated_request_paths(
    request_path: Path, payload: dict[str, object]
) -> tuple[list[str], dict[str, Path], float | None, int, int]:
    directory = request_path.parent.resolve()
    values = {
        "prompt_path": directory / "prompt.txt",
        "schema_path": directory / "output-schema.json",
        "stdout_path": directory / "stdout.jsonl",
        "result_path": directory / "last-message.json",
        "lock_path": directory / "supervisor.lock",
        "ready_path": directory / "supervisor-ready.json",
        "started_path": directory / "provider-started.json",
        "receipt_path": directory / "supervisor-exit.json",
        "stop_path": directory / "supervisor-stop.json",
    }
    stderr_path = directory / "provider-stderr.log"
    heartbeat_path = directory / "supervisor-heartbeat.json"
    argv = payload.get("argv")
    timeout_seconds = payload.get("timeout_seconds")
    prompt_max_bytes = payload.get("prompt_max_bytes")
    stream_max_bytes = payload.get("stream_max_bytes")
    result_max_bytes = payload.get("result_max_bytes")
    schema_ref = payload.get("schema_ref")
    expected_fields = {
        "schema_ref",
        "invocation_hash",
        "argv",
        "timeout_seconds",
        "stream_max_bytes",
        "result_max_bytes",
        *values,
    }
    if schema_ref == CODEX_SUPERVISOR_REQUEST_SCHEMA_V2:
        expected_fields.add("prompt_max_bytes")
    if "working_directory" in payload:
        expected_fields.add("working_directory")
        validated_working_directory(payload["working_directory"])
    operation_scope = payload.get("operation_scope")
    operation_scope_valid = True
    if "operation_scope" in payload:
        expected_fields.add("operation_scope")
        operation_scope_valid = (
            isinstance(operation_scope, dict)
            and bool(operation_scope)
            and set(operation_scope).issubset(_OPERATION_SCOPE_FIELDS)
            and all(
                isinstance(key, str)
                and isinstance(value, str)
                and bool(value)
                and len(value.encode("utf-8")) <= 512
                for key, value in operation_scope.items()
            )
        )
    # ``None`` is the normal long-running Provider policy: a valid operation
    # ends because the Provider exits, the Owner requests stop, or a bounded
    # output/process-integrity check fails.  A numeric wall-clock ceiling is an
    # explicit exceptional policy, not an implicit requirement of newer
    # transport schemas.
    timeout_valid = timeout_seconds is None
    if isinstance(timeout_seconds, (int, float)) and not isinstance(
        timeout_seconds, bool
    ):
        timeout_valid = 0 < float(timeout_seconds) <= (
            PROVIDER_SUPERVISOR_MAX_TIMEOUT_SECONDS
        )
    if (
        set(payload) != expected_fields
        or not operation_scope_valid
        or not isinstance(argv, list)
        or not argv
        or not all(isinstance(value, str) and value for value in argv)
        or not timeout_valid
        or not isinstance(stream_max_bytes, int)
        or isinstance(stream_max_bytes, bool)
        or not 0 < stream_max_bytes <= PROVIDER_SUPERVISOR_MAX_CONTENT_BYTES
        or not isinstance(result_max_bytes, int)
        or isinstance(result_max_bytes, bool)
        or not 0 < result_max_bytes <= PROVIDER_SUPERVISOR_MAX_CONTENT_BYTES
        or (
            schema_ref == CODEX_SUPERVISOR_REQUEST_SCHEMA_V2
            and (
                not isinstance(prompt_max_bytes, int)
                or isinstance(prompt_max_bytes, bool)
                or not 0
                < prompt_max_bytes
                <= PROVIDER_SUPERVISOR_MAX_CONTENT_BYTES
            )
        )
        or any(payload.get(name) != str(path) for name, path in values.items())
    ):
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    typed_argv = cast(list[str], argv)
    if "working_directory" in payload and "--cd" in typed_argv:
        try:
            cwd_arg = typed_argv[typed_argv.index("--cd") + 1]
        except IndexError as error:
            raise ProviderSupervisorError("provider_working_directory_invalid") from error
        if cwd_arg != payload["working_directory"]:
            raise ProviderSupervisorError("provider_working_directory_conflict")
    try:
        schema_arg = typed_argv[typed_argv.index("--output-schema") + 1]
        result_arg = typed_argv[typed_argv.index("--output-last-message") + 1]
    except (ValueError, IndexError) as error:
        raise ProviderSupervisorError("provider_supervisor_request_invalid") from error
    if (
        schema_arg != str(values["schema_path"])
        or result_arg != str(values["result_path"])
        or typed_argv[-1] != "-"
    ):
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    return (
        typed_argv,
        {**values, "stderr_path": stderr_path, "heartbeat_path": heartbeat_path},
        None if timeout_seconds is None else float(timeout_seconds),
        stream_max_bytes,
        result_max_bytes,
    )


def _terminate_provider(
    process: subprocess.Popen[bytes],
    *,
    process_platform: ProviderProcessPlatform,
    provider_job: ProviderProcessJob | None,
) -> int:
    if provider_job is None or not provider_job.terminate():
        raise ProviderSupervisorError("provider_job_termination_failed")
    if process.poll() is None:
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired as error:
            raise ProviderSupervisorError(
                "provider_process_termination_unconfirmed"
            ) from error
    assert process.returncode is not None
    return process.returncode


def _terminate_posix_process_group(process_group: int) -> bool:
    platform = ProviderProcessPlatform(platform_name="posix")
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 0.5
    while platform.process_group_running(process_group) and time.monotonic() < deadline:
        time.sleep(0.01)
    if platform.process_group_running(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + 0.5
        while (
            platform.process_group_running(process_group)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
    return not platform.process_group_running(process_group)


def _bounded_stdout_drain(
    stream,
    destination,
    maximum_bytes: int,
    exceeded: threading.Event,
    errors: list[BaseException],
) -> None:
    written = 0
    # BufferedReader.read(n) waits for n bytes or EOF, hiding short live replies.
    # read1 consumes the bytes currently available without filling the buffer.
    read_available = getattr(stream, "read1", stream.read)
    try:
        while chunk := read_available(64 * 1024):
            remaining = maximum_bytes - written
            if remaining > 0:
                accepted = chunk[:remaining]
                destination.write(accepted)
                written += len(accepted)
            if len(chunk) > max(remaining, 0):
                exceeded.set()
    except BaseException as error:
        errors.append(error)
    finally:
        stream.close()


def _close_descriptors(*descriptors: int | None) -> None:
    for descriptor in descriptors:
        if descriptor is None:
            continue
        try:
            os.close(descriptor)
        except OSError:
            pass


def _file_exceeds(path: Path, maximum_bytes: int) -> bool:
    try:
        return path.stat().st_size > maximum_bytes
    except FileNotFoundError:
        return False


def _seal_windows_result_file(
    path: Path,
    *,
    maximum_bytes: int,
    exceeded: threading.Event,
) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ProviderSupervisorError("provider_supervisor_spool_invalid")
    if not path.exists():
        with path.open("xb") as result:
            _make_private(path, result.fileno())
            result.flush()
            os.fsync(result.fileno())
        return
    with path.open("r+b", buffering=0) as result:
        _make_private(path, result.fileno())
        if path.stat().st_size > maximum_bytes:
            exceeded.set()
            result.truncate(maximum_bytes)
        os.fsync(result.fileno())


def _make_private(path: Path, descriptor: int) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    else:
        path.chmod(0o600)


def provider_result_argv(
    argv: list[str],
    *,
    platform_name: str | None = None,
    result_path: Path | None = None,
    result_write_fd: int | None = None,
    supervisor_process_id: int | None = None,
) -> list[str]:
    provider_argv = list(argv)
    try:
        result_index = provider_argv.index("--output-last-message") + 1
    except (ValueError, IndexError) as error:
        raise ProviderSupervisorError("provider_supervisor_request_invalid") from error
    selected_platform = platform_name or os.name
    if selected_platform == "posix":
        if result_path is not None:
            provider_argv[result_index] = str(result_path)
        elif result_write_fd is not None:
            owner_pid = (
                os.getpid()
                if supervisor_process_id is None
                else supervisor_process_id
            )
            provider_argv[result_index] = f"/proc/{owner_pid}/fd/{result_write_fd}"
        else:
            raise ProviderSupervisorError("provider_supervisor_result_pipe_invalid")
    elif selected_platform == "nt":
        if result_path is None:
            raise ProviderSupervisorError("provider_supervisor_result_path_invalid")
        provider_argv[result_index] = str(result_path)
    else:
        raise ProviderSupervisorError("provider_supervisor_platform_unsupported")
    return provider_argv


def _write_phase_marker(
    path: Path,
    *,
    schema_ref: str,
    invocation_hash: str,
    key: bytes,
    provider_process: subprocess.Popen[bytes] | None = None,
    operation_path: Path | None = None,
    process_platform: ProviderProcessPlatform | None = None,
) -> None:
    platform = process_platform or ProviderProcessPlatform()
    payload: dict[str, object] = {
        "schema_ref": schema_ref,
        "invocation_hash": invocation_hash,
        "supervisor_process_id": os.getpid(),
        "supervisor_process_group": platform.current_process_group(),
        **supervisor_host_identity(),
    }
    if provider_process is not None:
        if operation_path is None:
            raise ProviderSupervisorError("provider_process_identity_invalid")
        payload.update(
            {
                "provider_process_id": provider_process.pid,
                "provider_process_group": platform.process_group_for_pid(
                    provider_process.pid
                ),
                "provider_operation_path": str(operation_path.resolve()),
            }
        )
    _write_signed_envelope(path, payload, key)


def supervise(
    request_path: Path,
    *,
    process_platform: ProviderProcessPlatform | None = None,
    provider_job_factory: ProviderProcessJobFactory | None = None,
) -> None:
    request_path = request_path.resolve()
    platform = process_platform or ProviderProcessPlatform()
    key = _read_transport_key(_operation_key_path(request_path.parent))
    payload = read_supervisor_request(request_path, key)
    generic_schema = payload.get("schema_ref") == SUPERVISOR_REQUEST_SCHEMA_V2
    invocation_hash = payload.get("invocation_hash")
    if not isinstance(invocation_hash, str) or len(invocation_hash) != 64:
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    argv, paths, timeout_seconds, stream_max_bytes, result_max_bytes = (
        _validated_request_paths(request_path, payload)
    )
    stop_requested = False

    def request_stop(_signal_number: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    with SupervisorFileLock(paths["lock_path"]):
        if paths["receipt_path"].exists():
            return
        if (
            paths["started_path"].exists()
            or paths["ready_path"].exists()
            or paths["heartbeat_path"].exists()
            or paths["stderr_path"].exists()
        ):
            raise ProviderSupervisorError("provider_outcome_unknown")
        try:
            _supervise_locked(
                argv=argv,
                paths=paths,
                invocation_hash=invocation_hash,
                key=key,
                timeout_seconds=timeout_seconds,
                stream_max_bytes=stream_max_bytes,
                result_max_bytes=result_max_bytes,
                stop_requested=lambda: stop_requested
                or supervisor_stop_requested(
                    paths["stop_path"], key=key, invocation_hash=invocation_hash
                ),
                ready_schema_ref=(
                    "meta-research/provider-supervisor-ready/v2"
                    if generic_schema
                    else "meta-research/codex-provider-supervisor-ready/v1"
                ),
                started_schema_ref=(
                    "meta-research/provider-started/v2"
                    if generic_schema
                    else "meta-research/codex-provider-started/v2"
                ),
                exit_schema_ref=(
                    SUPERVISOR_EXIT_SCHEMA_V2
                    if generic_schema
                    else SUPERVISOR_EXIT_SCHEMA
                ),
                process_platform=platform,
                provider_job_factory=provider_job_factory,
                working_directory=(validated_working_directory(payload["working_directory"])
                    if "working_directory" in payload else None),
                operation_scope=(payload.get("operation_scope")
                    if isinstance(payload.get("operation_scope"), dict) else None),
            )
        except ProviderTreeError as error:
            raise ProviderSupervisorError(str(error)) from error


def _supervise_locked(
    *,
    argv: list[str],
    paths: dict[str, Path],
    invocation_hash: str,
    key: bytes,
    timeout_seconds: float | None,
    stream_max_bytes: int,
    result_max_bytes: int,
    stop_requested: Callable[[], bool],
    ready_schema_ref: str,
    started_schema_ref: str,
    exit_schema_ref: str,
    process_platform: ProviderProcessPlatform,
    provider_job_factory: ProviderProcessJobFactory | None,
    working_directory: Path | None = None,
    operation_scope: dict[str, object] | None = None,
) -> None:
    prompt_path = paths["prompt_path"]
    schema_path = paths["schema_path"]
    stdout_path = paths["stdout_path"]
    stderr_path = paths["stderr_path"]
    result_path = paths["result_path"]
    receipt_path = paths["receipt_path"]
    heartbeat_path = paths["heartbeat_path"]
    termination_reason = "completed"
    returncode: int | None = None
    native_started = False
    stdout_exceeded = threading.Event()
    stderr_exceeded = threading.Event()
    result_exceeded = threading.Event()
    stdout_errors: list[BaseException] = []
    stderr_errors: list[BaseException] = []
    result_errors: list[BaseException] = []
    result_temporary = result_path.with_name(".last-message.supervisor.tmp")
    if (
        stdout_path.exists()
        or stderr_path.exists()
        or result_path.exists()
        or result_temporary.exists()
    ):
        raise ProviderSupervisorError("provider_supervisor_spool_invalid")
    # Validate host identity before creating any spool files so a missing
    # machine-id or boot_id fails closed without leaving orphan empty files.
    supervisor_host_identity()
    selected_platform = process_platform.platform_name
    operation_path = (paths["ready_path"].parent / "supervisor-request.json").resolve()
    provider_environment = dict(os.environ)
    provider_environment[PROVIDER_OPERATION_ENV] = str(operation_path)
    if working_directory is not None:
        for name in ("META_RESEARCH_PROVIDER_CWD", "META_RESEARCH_HARNESS_WORKSPACE"):
            supplied = provider_environment.get(name)
            if supplied is not None and supplied != str(working_directory):
                raise ProviderSupervisorError("provider_working_directory_conflict")
        provider_environment["META_RESEARCH_PROVIDER_CWD"] = str(working_directory)
    ensure_provider_account_current(argv, provider_environment)
    invocation_codex_home: Path | None = None
    result_read_fd: int | None = None
    result_write_fd: int | None = None
    fifo: Path | None = None
    fifo_identity: os.stat_result | None = None
    process: subprocess.Popen[bytes] | None = None

    def write_heartbeat(
        phase: str,
        provider_process: subprocess.Popen[bytes] | None = None,
    ) -> None:
        # A lost heartbeat is an observation gap. It must not turn a provider
        # result into an unknown outcome or authorize a retry.
        try:
            _write_supervisor_heartbeat(
                heartbeat_path,
                invocation_hash=invocation_hash,
                key=key,
                phase=phase,
                process_platform=process_platform,
                provider_process=provider_process,
                operation_path=operation_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        except (OSError, ProviderSupervisorError):
            return
    stdout_drainer: threading.Thread | None = None
    stderr_drainer: threading.Thread | None = None
    result_drainer: threading.Thread | None = None
    result_source = None

    def close_result_pipe() -> None:
        nonlocal result_read_fd, result_write_fd
        _close_descriptors(result_read_fd, result_write_fd)
        result_read_fd = result_write_fd = None

    with ExitStack() as stack:
        # Cover setup failures too, before any stream or reader owns these FDs.
        stack.callback(close_result_pipe)
        if selected_platform == "posix":
            # Codex 0.169+ exec opens --output-last-message with O_NOFOLLOW,
            # which rejects Linux /proc/<pid>/fd/<fd> symlinks with ELOOP.
            # Keep every supervised POSIX provider on one private named FIFO
            # under the operation directory and reuse the bounded pipe drain.
            fifo_dir = Path(tempfile.mkdtemp(prefix=".result-pipe-", dir=result_path.parent))
            stack.callback(fifo_dir.rmdir)
            fifo = fifo_dir / "result"
            os.mkfifo(fifo, 0o600)
            stack.callback(fifo.unlink, missing_ok=True)
            fifo_identity = fifo.lstat()
            flags = os.O_NONBLOCK | os.O_NOFOLLOW
            result_read_fd = os.open(fifo, os.O_RDONLY | flags)
            result_write_fd = os.open(fifo, os.O_WRONLY | flags)
            if not stat.S_ISFIFO(fifo_identity.st_mode) or not all(
                os.path.samestat(fifo_identity, os.fstat(fd))
                for fd in (result_read_fd, result_write_fd)
            ):
                raise ProviderSupervisorError("provider_supervisor_result_pipe_invalid")
            os.set_blocking(result_read_fd, True)
            provider_argv = provider_result_argv(
                argv,
                platform_name=selected_platform,
                result_path=fifo,
            )
        elif selected_platform == "nt":
            provider_argv = provider_result_argv(
                argv,
                platform_name=selected_platform,
                result_path=result_temporary,
            )
        else:
            raise ProviderSupervisorError("provider_supervisor_platform_unsupported")
        provider_job: ProviderProcessJob | None = None
        if selected_platform == "nt":
            provider_job = (
                provider_job_factory or WindowsProviderJob
            )()
            stack.callback(provider_job.close)
        elif selected_platform == "posix":
            provider_job = LinuxProviderJob()
            stack.callback(provider_job.close)
        def cleanup_unready_spool(exc_type, _error, _traceback) -> None:
            if exc_type is not None and not paths["ready_path"].exists():
                for candidate in (stdout_path, result_temporary):
                    try:
                        if candidate.is_file() and candidate.stat().st_size == 0:
                            candidate.unlink(missing_ok=True)
                    except OSError:
                        pass

        stack.push(cleanup_unready_spool)
        prompt_stream = stack.enter_context(
            prompt_path.open("rb", buffering=0)
        )
        stdout_stream = stack.enter_context(
            stdout_path.open("xb", buffering=0)
        )
        result_stream = (
            stack.enter_context(result_temporary.open("xb", buffering=0))
            if selected_platform == "posix"
            else None
        )
        def finish_capture(exc_type, _error, _traceback) -> None:
            if exc_type is not None and process is not None:
                # A cleanup error must not leave the supervisor waiting forever,
                # nor turn an unconfirmed provider outcome into an exit receipt.
                try:
                    _terminate_provider(
                        process,
                        process_platform=process_platform,
                        provider_job=provider_job,
                    )
                except (OSError, ProviderSupervisorError, ProviderTreeError, subprocess.SubprocessError):
                    pass  # Preserve the original error and the unsealed spool.
            close_result_pipe()
            for drainer in (stdout_drainer, stderr_drainer, result_drainer):
                if drainer is not None and drainer.ident is not None:
                    drainer.join(timeout=1.0)
            if result_source is not None and (
                result_drainer is None or result_drainer.ident is None
            ):
                result_source.close()
            if process is not None and process.stdout is not None and (
                stdout_drainer is None or stdout_drainer.ident is None
            ):
                process.stdout.close()
            provider_stderr = getattr(process, "stderr", None)
            if provider_stderr is not None and (
                stderr_drainer is None or stderr_drainer.ident is None
            ):
                provider_stderr.close()

        _write_phase_marker(
            paths["ready_path"], schema_ref=ready_schema_ref,
            invocation_hash=invocation_hash, key=key, process_platform=process_platform,
        )
        # Create provider-stderr.log only after supervisor-ready.json exists so
        # startup failures never leave an orphan stderr sidecar blocking recovery.
        stderr_stream = stack.enter_context(
            stderr_path.open("xb", buffering=0)
        )
        # Run before ExitStack closes stdout/stderr/result streams.
        stack.push(finish_capture)
        write_heartbeat("ready")
        if stop_requested():
            termination_reason = "stopped"
            close_result_pipe()
        else:
            try:
                if provider_environment.get("CODEX_HOME"):
                    invocation_codex_home = Path(provider_environment["CODEX_HOME"])
                spawn_options: dict[str, object] = {
                    "stdin": prompt_stream,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "env": provider_environment,
                    "cwd": working_directory,
                    **process_platform.provider_spawn_options(),
                }
                process = (
                    provider_job.spawn(provider_argv, **spawn_options)
                    if provider_job is not None
                    else subprocess.Popen(provider_argv, **spawn_options)
                )
            except OSError:
                close_result_pipe()
                termination_reason = "launch_failed"
            else:
                native_started = True
                _write_phase_marker(
                    paths["started_path"],
                    schema_ref=started_schema_ref,
                    invocation_hash=invocation_hash,
                    key=key,
                    provider_process=process,
                    operation_path=operation_path,
                    process_platform=process_platform,
                )
                write_heartbeat("running", process)
                assert process.stdout is not None
                provider_stderr = getattr(process, "stderr", None)
                stdout_drainer = threading.Thread(
                    daemon=True,
                    target=_bounded_stdout_drain,
                    args=(
                        process.stdout,
                        stdout_stream,
                        stream_max_bytes,
                        stdout_exceeded,
                        stdout_errors,
                    ),
                )
                if provider_stderr is not None:
                    stderr_drainer = threading.Thread(
                        daemon=True,
                        target=_bounded_stdout_drain,
                        args=(
                            provider_stderr,
                            stderr_stream,
                            stream_max_bytes,
                            stderr_exceeded,
                            stderr_errors,
                        ),
                    )
                if result_read_fd is not None:
                    result_source = os.fdopen(result_read_fd, "rb", buffering=0)
                    result_read_fd = None  # Ownership transferred to the reader.
                result_drainer = (
                    threading.Thread(
                        daemon=True,
                        target=_bounded_stdout_drain,
                        args=(
                            result_source,
                            result_stream,
                            result_max_bytes,
                            result_exceeded,
                            result_errors,
                        ),
                    )
                    if result_source is not None and result_stream is not None
                    else None
                )
                stdout_drainer.start()
                if stderr_drainer is not None:
                    stderr_drainer.start()
                if result_drainer is not None:
                    result_drainer.start()
                deadline = (
                    None
                    if timeout_seconds is None
                    else time.monotonic() + timeout_seconds
                )
                next_heartbeat_at = time.monotonic() + SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS
                while process.poll() is None or (
                    provider_job is not None
                    and provider_job.active_process_count() > 0
                ):
                    if stop_requested():
                        termination_reason = "stopped"
                        returncode = _terminate_provider(
                            process,
                            process_platform=process_platform,
                            provider_job=provider_job,
                        )
                        break
                    if time.monotonic() >= next_heartbeat_at:
                        write_heartbeat("running", process)
                        next_heartbeat_at = time.monotonic() + SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS
                    if deadline is not None and time.monotonic() >= deadline:
                        termination_reason = "timeout"
                        returncode = _terminate_provider(
                            process,
                            process_platform=process_platform,
                            provider_job=provider_job,
                        )
                        break
                    if (
                        stdout_exceeded.is_set()
                        or result_exceeded.is_set()
                    ):
                        termination_reason = "output_limit"
                        returncode = _terminate_provider(
                            process,
                            process_platform=process_platform,
                            provider_job=provider_job,
                        )
                        break
                    if (
                        selected_platform == "nt"
                        and _file_exceeds(result_temporary, result_max_bytes)
                    ):
                        result_exceeded.set()
                        continue
                    if process.poll() is None:
                        try:
                            process.wait(timeout=0.05)
                        except subprocess.TimeoutExpired:
                            continue
                    else:
                        time.sleep(0.05)
                if termination_reason == "completed":
                    assert process.returncode is not None
                    returncode = process.returncode
                if isinstance(provider_job, LinuxProviderJob):
                    native_result = provider_job.result()
                    returncode = cast(int | None, native_result["returncode"])
                    native_started = bool(native_result["native_started"])
                    if native_result["launch_failed"]:
                        termination_reason = "launch_failed"
                    elif not native_started:
                        termination_reason = "stopped"
                    elif termination_reason == "completed" and native_result["descendants_terminated"]:
                        termination_reason = "descendant_process"
                close_result_pipe()
                stdout_drainer.join(timeout=1.0)
                if stderr_drainer is not None:
                    stderr_drainer.join(timeout=1.0)
                if result_drainer is not None:
                    result_drainer.join(timeout=1.0)
                if stdout_drainer.is_alive() or (
                    stderr_drainer is not None and stderr_drainer.is_alive()
                ) or (
                    result_drainer is not None and result_drainer.is_alive()
                ):
                    if provider_job is not None:
                        if not provider_job.terminate():
                            raise ProviderSupervisorError(
                                "provider_job_termination_failed"
                            )
                    stdout_drainer.join(timeout=1.0)
                    if stderr_drainer is not None:
                        stderr_drainer.join(timeout=1.0)
                    if result_drainer is not None:
                        result_drainer.join(timeout=1.0)
                if (
                    stdout_drainer.is_alive()
                    or (
                        stderr_drainer is not None
                        and stderr_drainer.is_alive()
                    )
                    or (
                        result_drainer is not None
                        and result_drainer.is_alive()
                    )
                    or stdout_errors
                    or stderr_errors
                    or result_errors
                ):
                    raise ProviderSupervisorError(
                        "provider_stdout_capture_failed"
                    )
                if (
                    stdout_exceeded.is_set()
                    or result_exceeded.is_set()
                ):
                    termination_reason = "output_limit"
        if (
            provider_job is not None
            and provider_job.active_process_count() != 0
        ):
            raise ProviderSupervisorError("provider_job_not_empty")
        os.fsync(stdout_stream.fileno())
        os.fsync(stderr_stream.fileno())
        if fifo is not None:
            try:
                observed_fifo = fifo.lstat()
            except OSError as error:
                raise ProviderSupervisorError("provider_supervisor_result_pipe_invalid") from error
            if not os.path.samestat(fifo_identity, observed_fifo):
                raise ProviderSupervisorError("provider_supervisor_result_pipe_invalid")
        if result_stream is not None:
            os.fsync(result_stream.fileno())
        input_bytes = prompt_stream.tell()
    if selected_platform == "nt":
        _seal_windows_result_file(
            result_temporary,
            maximum_bytes=result_max_bytes,
            exceeded=result_exceeded,
        )
        if result_exceeded.is_set():
            termination_reason = "output_limit"
    os.replace(result_temporary, result_path)
    _fsync_directory(result_path.parent)
    if (
        stdout_path.stat().st_size > stream_max_bytes
        or result_path.stat().st_size > result_max_bytes
    ):
        termination_reason = "output_limit"
    write_exit_receipt(
        receipt_path,
        key=key,
        invocation_hash=invocation_hash,
        prompt_path=prompt_path,
        schema_path=schema_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stderr_limited=(
            stderr_exceeded.is_set()
            or (stderr_path.is_file() and stderr_path.stat().st_size > stream_max_bytes)
        ),
        result_path=result_path,
        returncode=returncode,
        input_bytes=input_bytes,
        process_tree={
            "schema_ref": "meta-research/provider-process-tree/v1", "state": "closed",
            "mechanism": ("not-started" if not native_started else
                          "linux-subreaper-echild" if selected_platform == "posix" else "windows-job"),
            **supervisor_host_identity(),
        },
        termination_reason=termination_reason,
        schema_ref=exit_schema_ref,
        codex_home=invocation_codex_home,
        operation_scope=operation_scope,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 64
    try:
        supervise(Path(arguments[0]))
    except (OSError, ProviderSupervisorError, ProviderTreeError, subprocess.SubprocessError) as error:
        emit_supervisor_error(error)
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
