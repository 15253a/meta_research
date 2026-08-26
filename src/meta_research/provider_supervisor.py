from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast


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


def minimal_subprocess_environment(
    *,
    platform_name: str | None = None,
    source_environment: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Keep only host bootstrap variables required by an isolated subprocess."""

    selected_platform = platform_name or os.name
    source = os.environ if source_environment is None else source_environment
    allowed = {"PATH"}
    if selected_platform == "nt":
        allowed.update(
            {
                "COMSPEC",
                "PATHEXT",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "WINDIR",
            }
        )
    elif selected_platform != "posix":
        raise ProviderSupervisorError("provider_supervisor_platform_unsupported")
    environment = {
        name: value
        for name, value in source.items()
        if name.upper() in allowed
    }
    if not any(name.upper() == "PATH" for name in environment):
        environment["PATH"] = ""
    if extra is not None:
        environment.update(extra)
    return environment


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
    if receipt_path.is_file():
        return True
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
        not in {frozenset(ready_keys), frozenset({*ready_keys, "phase"})}
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
    supervisor_running = False
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
            supervisor_running = True
            try:
                os.kill(process_id, signal.SIGTERM)
            except ProcessLookupError:
                supervisor_running = False
    elif platform.platform_name == "nt":
        # The exact sealed operation file is the Windows control channel. Do
        # not signal a possibly reused PID merely because its numeric value is
        # present in an old marker.
        write_supervisor_stop_request(
            operation_directory / "supervisor-stop.json",
            key=key,
            invocation_hash=invocation_hash,
        )
        supervisor_running = platform.process_group_running(process_id)
    else:
        raise ProviderSupervisorError("provider_supervisor_platform_unsupported")
    deadline = time.monotonic() + wait_seconds
    while not receipt_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    if receipt_path.is_file():
        return True
    if platform.platform_name == "posix":
        try:
            supervisor_running = (
                platform.process_group_for_pid(process_id) == process_group
            )
        except ProcessLookupError:
            supervisor_running = False
        except OSError as error:
            raise ProviderSupervisorError(
                "provider_supervisor_identity_invalid"
            ) from error
    else:
        supervisor_running = platform.process_group_running(process_id)
    if supervisor_running:
        return False
    return _terminate_or_verify_bound_provider_absent(
        operation_directory,
        key=key,
        invocation_hash=invocation_hash,
        process_platform=platform,
    )


def supervisor_request_never_started(
    operation_directory: Path,
    *,
    key: bytes,
    invocation_hash: str,
    request_schema: str,
    now: float | None = None,
    platform_name: str | None = None,
    supervisor_lock_held: Callable[[Path], bool] | None = None,
) -> bool:
    """Prove that a durable request aged past its launch window without Popen.

    The supervisor protocol publishes ``ready`` before ``started`` and before
    the provider Popen.  Age alone is not proof, however: the daemon that owned
    the five-second readiness deadline may have died while a detached supervisor
    was scheduled but had not yet published ``ready``.  We therefore also prove
    that no same-user live process is bound to the exact request path.
    """

    request_path = operation_directory / "supervisor-request.json"
    if not request_path.is_file():
        return True
    request = read_transport_envelope(request_path, key)
    if (
        request.get("schema_ref") != request_schema
        or request.get("invocation_hash") != invocation_hash
    ):
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    if any(
        (operation_directory / name).exists()
        for name in (
            "supervisor-ready.json",
            "provider-started.json",
            "supervisor-exit.json",
        )
    ):
        return False
    observed_at = time.time() if now is None else now
    if (
        observed_at - request_path.stat().st_mtime
        < SUPERVISOR_STARTUP_GRACE_SECONDS
    ):
        return False
    selected_platform = platform_name or os.name
    if selected_platform == "nt":
        lock_probe = supervisor_lock_held or (
            lambda path: _supervisor_lock_is_held(
                path,
                platform_name=selected_platform,
            )
        )
        return not lock_probe(operation_directory / "supervisor.lock")
    if selected_platform != "posix":
        raise ProviderSupervisorError("provider_supervisor_platform_unsupported")
    return not _supervisor_processes_for_request(request_path.resolve())


def _supervisor_lock_is_held(path: Path, *, platform_name: str) -> bool:
    lock = SupervisorFileLock(path, platform_name=platform_name)
    if not lock.acquire(blocking=False):
        return True
    lock.release()
    return False


def _supervisor_processes_for_request(request_path: Path) -> set[int]:
    """Find same-user live supervisors carrying one exact durable request path."""

    request_token = str(request_path).encode("utf-8")
    process_ids: set[int] = set()
    current_uid = os.getuid()
    for process_directory in Path("/proc").glob("[0-9]*"):
        try:
            metadata = process_directory.stat()
            if metadata.st_uid != current_uid:
                continue
            command_line = (process_directory / "cmdline").read_bytes().split(b"\0")
            if request_token not in command_line:
                continue
            process_id = int(process_directory.name)
            os.getpgid(process_id)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as error:
            # Same-uid identity becoming unreadable is an unknown effect, not
            # evidence that the supervisor never launched.
            raise ProviderSupervisorError(
                "provider_supervisor_identity_unavailable"
            ) from error
        except OSError as error:
            if error.errno in {errno.ENOENT, errno.ESRCH}:
                continue
            raise ProviderSupervisorError(
                "provider_supervisor_identity_unavailable"
            ) from error
        except ValueError as error:
            raise ProviderSupervisorError(
                "provider_supervisor_identity_unavailable"
            ) from error
        if process_id <= 1:
            raise ProviderSupervisorError("provider_supervisor_identity_invalid")
        process_ids.add(process_id)
    return process_ids


def _terminate_or_verify_bound_provider_absent(
    operation_directory: Path,
    *,
    key: bytes,
    invocation_hash: str,
    process_platform: ProviderProcessPlatform | None = None,
) -> bool:
    """Terminate provider groups bound to this exact sealed operation path."""

    platform = process_platform or ProviderProcessPlatform()
    request_path = (operation_directory / "supervisor-request.json").resolve()
    marker_path = operation_directory / "provider-started.json"
    marker_group: int | None = None
    if marker_path.is_file():
        marker = read_transport_envelope(marker_path, key)
        base_keys = {
            "schema_ref",
            "invocation_hash",
            "supervisor_process_id",
            "supervisor_process_group",
            "provider_process_id",
            "provider_process_group",
            "provider_operation_path",
        }
        if (
            frozenset(marker)
            not in {frozenset(base_keys), frozenset({*base_keys, "phase"})}
            or marker.get("invocation_hash") != invocation_hash
            or marker.get("schema_ref")
            not in {
                "meta-research/codex-provider-started/v2",
                "meta-research/provider-started/v2",
                "meta-research/experiment-provider-phase/v2",
            }
            or ("phase" in marker and marker.get("phase") != "started")
            or marker.get("provider_operation_path") != str(request_path)
            or not isinstance(marker.get("provider_process_id"), int)
            or isinstance(marker.get("provider_process_id"), bool)
            or int(marker["provider_process_id"]) <= 1
            or not isinstance(marker.get("provider_process_group"), int)
            or isinstance(marker.get("provider_process_group"), bool)
            or marker.get("provider_process_group")
            != marker.get("provider_process_id")
        ):
            raise ProviderSupervisorError("provider_started_marker_invalid")
        marker_group = int(marker["provider_process_group"])

    groups = _provider_groups_for_operation(
        request_path,
        platform_name=platform.platform_name,
    )
    if marker_group is not None and platform.process_group_running(marker_group):
        # A reused numeric PGID is never enough: at least one live group member
        # must still carry the exact operation token inherited at Popen.
        if marker_group not in groups:
            return False
        groups.add(marker_group)
    for group in groups:
        if not platform.terminate_process_group(group):
            return False
    remaining = _provider_groups_for_operation(
        request_path,
        platform_name=platform.platform_name,
    )
    if remaining:
        return False
    return marker_group is None or not platform.process_group_running(marker_group)


def _provider_groups_for_operation(
    request_path: Path,
    *,
    platform_name: str | None = None,
) -> set[int]:
    if (platform_name or os.name) != "posix":
        # A Windows PID marker without a creation identity is not sufficient
        # authority to terminate a possibly reused process. Keep reconciliation
        # pending if the owning supervisor did not seal a receipt.
        return set()
    token = f"{PROVIDER_OPERATION_ENV}={request_path}".encode("utf-8")
    groups: set[int] = set()
    for process_directory in Path("/proc").glob("[0-9]*"):
        try:
            values = (process_directory / "environ").read_bytes().split(b"\0")
            if token not in values:
                continue
            process_id = int(process_directory.name)
            process_group = os.getpgid(process_id)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EPERM, errno.ENOENT, errno.ESRCH}:
                continue
            raise ProviderSupervisorError(
                "provider_process_identity_unavailable"
            ) from error
        except ValueError as error:
            raise ProviderSupervisorError(
                "provider_process_identity_unavailable"
            ) from error
        if process_id <= 1 or process_group <= 1:
            raise ProviderSupervisorError("provider_process_identity_invalid")
        groups.add(process_group)
    return groups


def read_transport_key_for_operation(
    operation_directory: Path,
) -> tuple[Path, bytes]:
    key_path = _operation_key_path(operation_directory)
    return key_path, _read_transport_key(key_path)


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


def write_exit_receipt(
    path: Path,
    *,
    key: bytes,
    invocation_hash: str,
    prompt_path: Path,
    schema_path: Path,
    stdout_path: Path,
    result_path: Path,
    returncode: int,
    input_bytes: int,
    termination_reason: str = "completed",
    schema_ref: str = SUPERVISOR_EXIT_SCHEMA,
) -> None:
    if schema_ref not in _SUPERVISOR_EXIT_SCHEMAS:
        raise ProviderSupervisorError("provider_supervisor_exit_invalid")
    prompt_bytes = prompt_path.stat().st_size
    payload: dict[str, object] = {
        "schema_ref": schema_ref,
        "invocation_hash": invocation_hash,
        "returncode": returncode,
        "termination_reason": termination_reason,
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
    }
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
        or not isinstance(returncode, int)
        or isinstance(returncode, bool)
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


def _validated_request_paths(
    request_path: Path, payload: dict[str, object]
) -> tuple[list[str], dict[str, Path], float, int, int]:
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
    if (
        set(payload) != expected_fields
        or not isinstance(argv, list)
        or not argv
        or not all(isinstance(value, str) and value for value in argv)
        or not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 0
        < float(timeout_seconds)
        <= PROVIDER_SUPERVISOR_MAX_TIMEOUT_SECONDS
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
        values,
        float(timeout_seconds),
        stream_max_bytes,
        result_max_bytes,
    )


def _terminate_provider(
    process: subprocess.Popen[bytes],
    *,
    process_platform: ProviderProcessPlatform,
    provider_job: ProviderProcessJob | None,
) -> int:
    if provider_job is not None:
        if not provider_job.terminate():
            raise ProviderSupervisorError("provider_job_termination_failed")
    elif process_platform.platform_name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        raise ProviderSupervisorError("provider_windows_job_required")
    if process.poll() is None:
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired as error:
            if process_platform.platform_name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1.0)
            else:
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
    try:
        while chunk := stream.read(64 * 1024):
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
        if result_write_fd is None:
            raise ProviderSupervisorError("provider_supervisor_result_pipe_invalid")
        owner_pid = (
            os.getpid()
            if supervisor_process_id is None
            else supervisor_process_id
        )
        provider_argv[result_index] = f"/proc/{owner_pid}/fd/{result_write_fd}"
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
        if paths["started_path"].exists():
            raise ProviderSupervisorError("provider_outcome_unknown")
        _write_phase_marker(
            paths["ready_path"],
            schema_ref=(
                "meta-research/provider-supervisor-ready/v2"
                if generic_schema
                else "meta-research/codex-provider-supervisor-ready/v1"
            ),
            invocation_hash=invocation_hash,
            key=key,
            process_platform=platform,
        )
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
        )


def _supervise_locked(
    *,
    argv: list[str],
    paths: dict[str, Path],
    invocation_hash: str,
    key: bytes,
    timeout_seconds: float,
    stream_max_bytes: int,
    result_max_bytes: int,
    stop_requested: Callable[[], bool],
    started_schema_ref: str,
    exit_schema_ref: str,
    process_platform: ProviderProcessPlatform,
    provider_job_factory: ProviderProcessJobFactory | None,
) -> None:
    prompt_path = paths["prompt_path"]
    schema_path = paths["schema_path"]
    stdout_path = paths["stdout_path"]
    result_path = paths["result_path"]
    receipt_path = paths["receipt_path"]
    termination_reason = "completed"
    returncode = 143
    stdout_exceeded = threading.Event()
    result_exceeded = threading.Event()
    stdout_errors: list[BaseException] = []
    result_errors: list[BaseException] = []
    result_temporary = result_path.with_name(".last-message.supervisor.tmp")
    if (
        stdout_path.exists()
        or result_path.exists()
        or result_temporary.exists()
    ):
        raise ProviderSupervisorError("provider_supervisor_spool_invalid")
    selected_platform = process_platform.platform_name
    result_read_fd: int | None = None
    result_write_fd: int | None = None
    if selected_platform == "posix":
        result_read_fd, result_write_fd = os.pipe()
        provider_argv = provider_result_argv(
            argv,
            platform_name=selected_platform,
            result_write_fd=result_write_fd,
        )
    elif selected_platform == "nt":
        provider_argv = provider_result_argv(
            argv,
            platform_name=selected_platform,
            result_path=result_temporary,
        )
    else:
        raise ProviderSupervisorError("provider_supervisor_platform_unsupported")
    with ExitStack() as stack:
        provider_job: ProviderProcessJob | None = None
        if selected_platform == "nt":
            provider_job = (
                provider_job_factory or WindowsProviderJob
            )()
            stack.callback(provider_job.close)
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
        if stop_requested():
            termination_reason = "stopped"
            _close_descriptors(result_read_fd, result_write_fd)
        else:
            try:
                operation_path = (
                    paths["ready_path"].parent / "supervisor-request.json"
                ).resolve()
                provider_environment = dict(os.environ)
                provider_environment[PROVIDER_OPERATION_ENV] = str(operation_path)
                spawn_options: dict[str, object] = {
                    "stdin": prompt_stream,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.DEVNULL,
                    "env": provider_environment,
                    **process_platform.provider_spawn_options(),
                }
                process = (
                    provider_job.spawn(provider_argv, **spawn_options)
                    if provider_job is not None
                    else subprocess.Popen(provider_argv, **spawn_options)
                )
            except OSError:
                _close_descriptors(result_read_fd, result_write_fd)
                termination_reason = "launch_failed"
                returncode = 127
            else:
                _write_phase_marker(
                    paths["started_path"],
                    schema_ref=started_schema_ref,
                    invocation_hash=invocation_hash,
                    key=key,
                    provider_process=process,
                    operation_path=operation_path,
                    process_platform=process_platform,
                )
                assert process.stdout is not None
                stdout_drainer = threading.Thread(
                    target=_bounded_stdout_drain,
                    args=(
                        process.stdout,
                        stdout_stream,
                        stream_max_bytes,
                        stdout_exceeded,
                        stdout_errors,
                    ),
                )
                result_drainer = (
                    threading.Thread(
                        target=_bounded_stdout_drain,
                        args=(
                            os.fdopen(result_read_fd, "rb", buffering=0),
                            result_stream,
                            result_max_bytes,
                            result_exceeded,
                            result_errors,
                        ),
                    )
                    if result_read_fd is not None and result_stream is not None
                    else None
                )
                stdout_drainer.start()
                if result_drainer is not None:
                    result_drainer.start()
                deadline = time.monotonic() + timeout_seconds
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
                    if time.monotonic() >= deadline:
                        termination_reason = "timeout"
                        returncode = _terminate_provider(
                            process,
                            process_platform=process_platform,
                            provider_job=provider_job,
                        )
                        break
                    if stdout_exceeded.is_set() or result_exceeded.is_set():
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
                if provider_job is None and process_platform.process_group_running(
                    process.pid
                ):
                    termination_reason = "descendant_process"
                    if not process_platform.terminate_process_group(process.pid):
                        raise ProviderSupervisorError(
                            "provider_descendant_cleanup_failed"
                        )
                _close_descriptors(result_write_fd)
                stdout_drainer.join(timeout=1.0)
                if result_drainer is not None:
                    result_drainer.join(timeout=1.0)
                if stdout_drainer.is_alive() or (
                    result_drainer is not None and result_drainer.is_alive()
                ):
                    if provider_job is not None:
                        if not provider_job.terminate():
                            raise ProviderSupervisorError(
                                "provider_job_termination_failed"
                            )
                    else:
                        process_platform.terminate_process_group(process.pid)
                    stdout_drainer.join(timeout=1.0)
                    if result_drainer is not None:
                        result_drainer.join(timeout=1.0)
                if (
                    stdout_drainer.is_alive()
                    or (
                        result_drainer is not None
                        and result_drainer.is_alive()
                    )
                    or stdout_errors
                    or result_errors
                ):
                    raise ProviderSupervisorError(
                        "provider_stdout_capture_failed"
                    )
                if stdout_exceeded.is_set() or result_exceeded.is_set():
                    termination_reason = "output_limit"
        if (
            provider_job is not None
            and provider_job.active_process_count() != 0
        ):
            raise ProviderSupervisorError("provider_job_not_empty")
        os.fsync(stdout_stream.fileno())
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
        result_path=result_path,
        returncode=returncode,
        input_bytes=input_bytes,
        termination_reason=termination_reason,
        schema_ref=exit_schema_ref,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 64
    try:
        supervise(Path(arguments[0]))
    except (OSError, ProviderSupervisorError, subprocess.SubprocessError):
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
