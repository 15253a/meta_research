from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import meta_research.provider_supervisor as provider_supervisor_module
from meta_research.experiment_provider_supervisor import (
    REQUEST_SCHEMA as EXPERIMENT_REQUEST_SCHEMA,
    ensure_transport_key as ensure_experiment_transport_key,
    read_signed as read_experiment_signed,
    supervise as supervise_experiment,
    write_signed as write_experiment_signed,
)
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2,
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ProviderProcessPlatform,
    ProviderSupervisorError,
    SupervisorFileLock,
    WindowsProviderJob,
    ensure_transport_key,
    minimal_subprocess_environment,
    provider_result_argv,
    read_verified_exit_receipt,
    request_supervisor_stop,
    supervise as supervise_provider,
    supervisor_request_never_started,
    supervisor_stop_requested,
    write_supervisor_request,
    write_supervisor_stop_request,
    write_transport_envelope,
)


class _ExitedRootProcess:
    def __init__(self) -> None:
        self.pid = 8124
        self.returncode = 0
        self.stdout = io.BytesIO()

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0


class _FakeWindowsJob:
    def __init__(self) -> None:
        self.spawned = threading.Event()
        self.terminated = False
        self.closed = False
        self._active_processes = 0

    def spawn(self, argv: list[str], **options: object) -> _ExitedRootProcess:
        del argv, options
        self._active_processes = 1
        self.spawned.set()
        return _ExitedRootProcess()

    def active_process_count(self) -> int:
        return self._active_processes

    def terminate(self, exit_code: int = 1) -> bool:
        del exit_code
        self.terminated = True
        self._active_processes = 0
        return True

    def close(self) -> None:
        self.closed = True
        self._active_processes = 0


class _FakeWindowsFunction:
    def __init__(self, implementation) -> None:
        self._implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._implementation(*args)


def test_provider_supervisor_modules_import_without_fcntl() -> None:
    script = """
import builtins
import os
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "fcntl":
        raise ImportError("fcntl deliberately unavailable")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
for posix_only_name in ("fchmod", "getpgid", "getpgrp", "getuid", "killpg"):
    if hasattr(os, posix_only_name):
        delattr(os, posix_only_name)
from meta_research.provider_supervisor import SupervisorFileLock
from meta_research.experiment_provider_supervisor import supervise
from meta_research.composition import build_production_runtime
assert SupervisorFileLock.__name__ == "SupervisorFileLock"
assert callable(supervise)
assert callable(build_production_runtime)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert (completed.returncode, completed.stderr) == (0, "")


def test_windows_supervisor_lock_and_process_control_are_protocol_testable(
    tmp_path: Path,
) -> None:
    lock_calls: list[tuple[bool, bool]] = []

    def lock_operation(_descriptor: int, acquire: bool, blocking: bool) -> None:
        lock_calls.append((acquire, blocking))

    lock = SupervisorFileLock(
        tmp_path / "supervisor.lock",
        platform_name="nt",
        lock_operation=lock_operation,
    )
    assert lock.acquire(blocking=False)
    lock.release()
    assert (tmp_path / "supervisor.lock").stat().st_size == 1
    assert lock_calls == [(True, False), (False, False)]

    inspected: list[int] = []
    def pid_probe(pid: int) -> bool:
        inspected.append(pid)
        return True

    platform = ProviderProcessPlatform(
        platform_name="nt",
        windows_pid_probe=pid_probe,
        create_new_process_group=0x200,
        detached_process=0x8,
    )
    assert platform.provider_spawn_options() == {"creationflags": 0x200}
    assert platform.supervisor_spawn_options() == {
        "close_fds": True,
        "creationflags": 0x208,
    }
    assert platform.process_group_for_pid(8123) == 8123
    assert platform.process_group_running(8123)
    assert inspected == [8123]
    with pytest.raises(
        ProviderSupervisorError,
        match="provider_windows_job_required",
    ):
        platform.terminate_process_group(8123)


def test_windows_job_binds_suspended_process_before_resume_and_owns_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    events: list[tuple[object, ...]] = []
    active_processes = 2

    class FakeKernel32:
        def __init__(self) -> None:
            self.CreateJobObjectW = _FakeWindowsFunction(
                lambda _attributes, _name: 91
            )
            self.SetInformationJobObject = _FakeWindowsFunction(
                self._set_information
            )
            self.AssignProcessToJobObject = _FakeWindowsFunction(
                lambda job, process: events.append(
                    ("assign", int(job), int(process.value))
                )
                or 1
            )
            self.QueryInformationJobObject = _FakeWindowsFunction(
                self._query_information
            )
            self.TerminateJobObject = _FakeWindowsFunction(
                self._terminate_job
            )
            self.TerminateProcess = _FakeWindowsFunction(
                lambda process, code: events.append(
                    ("terminate-process", int(process.value), code)
                )
                or 1
            )
            self.CloseHandle = _FakeWindowsFunction(
                lambda handle: events.append(("close", int(handle))) or 1
            )

        def _set_information(
            self,
            job,
            information_class,
            information,
            _size,
        ) -> int:
            events.append(
                (
                    "set-limit",
                    int(job),
                    information_class,
                    information._obj.BasicLimitInformation.LimitFlags,
                )
            )
            return 1

        def _query_information(
            self,
            _job,
            _information_class,
            information,
            _size,
            _returned_length,
        ) -> int:
            information._obj.ActiveProcesses = active_processes
            return 1

        def _terminate_job(self, job, exit_code) -> int:
            nonlocal active_processes
            events.append(("terminate-job", int(job), exit_code))
            active_processes = 0
            return 1

    class FakeNtdll:
        def __init__(self) -> None:
            self.NtResumeProcess = _FakeWindowsFunction(
                lambda process: events.append(
                    ("resume", int(process.value))
                )
                or 0
            )

    class FakeProcess:
        pid = 8125
        returncode = None
        _handle = 502

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    kernel32 = FakeKernel32()
    ntdll = FakeNtdll()

    def load_library(name: str, *, use_last_error: bool):
        assert use_last_error
        if name == "kernel32":
            return kernel32
        if name == "ntdll":
            return ntdll
        raise AssertionError(name)

    def spawn(argv: list[str], **options: object) -> FakeProcess:
        events.append(("spawn", tuple(argv), options["creationflags"]))
        return FakeProcess()

    monkeypatch.setattr(provider_supervisor_module.os, "name", "nt")
    monkeypatch.setattr(ctypes, "WinDLL", load_library, raising=False)
    monkeypatch.setattr(provider_supervisor_module.subprocess, "Popen", spawn)

    job = WindowsProviderJob()
    process = job.spawn(["provider"], creationflags=0x200)

    assert process.pid == 8125
    assert events[:4] == [
        ("set-limit", 91, 9, 0x2000),
        ("spawn", ("provider",), 0x204),
        ("assign", 91, 502),
        ("resume", 502),
    ]
    assert job.active_process_count() == 2
    assert job.terminate(exit_code=143)
    assert job.active_process_count() == 0
    job.close()
    assert events[-2:] == [("terminate-job", 91, 143), ("close", 91)]


def test_windows_stop_uses_the_sealed_operation_control_without_proc(
    tmp_path: Path,
) -> None:
    operation = tmp_path / "provider-operations" / "operation" / "phase"
    operation.mkdir(parents=True)
    invocation_hash = "a" * 64
    key = b"k" * 32
    ready_schema = "meta-research/provider-supervisor-ready/v2"
    write_transport_envelope(
        operation / "supervisor-ready.json",
        {
            "schema_ref": ready_schema,
            "invocation_hash": invocation_hash,
            "supervisor_process_id": 9123,
            "supervisor_process_group": 9123,
        },
        key,
    )
    stop_seen = threading.Event()

    def acknowledge_stop() -> None:
        stop_path = operation / "supervisor-stop.json"
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if supervisor_stop_requested(
                stop_path,
                key=key,
                invocation_hash=invocation_hash,
            ):
                stop_seen.set()
                (operation / "supervisor-exit.json").touch()
                return
            time.sleep(0.01)

    observer = threading.Thread(target=acknowledge_stop)
    observer.start()
    platform = ProviderProcessPlatform(
        platform_name="nt",
        windows_pid_probe=lambda pid: pid == 9123,
    )
    try:
        assert request_supervisor_stop(
            operation,
            key=key,
            invocation_hash=invocation_hash,
            ready_schema=ready_schema,
            wait_seconds=1,
            process_platform=platform,
        )
    finally:
        observer.join(timeout=2)

    assert stop_seen.is_set()


def test_windows_minimal_environment_keeps_bootstrap_values_without_secrets() -> None:
    environment = minimal_subprocess_environment(
        platform_name="nt",
        source_environment={
            "PATH": r"C:\Windows\System32",
            "SystemRoot": r"C:\Windows",
            "COMSPEC": r"C:\Windows\System32\cmd.exe",
            "PATHEXT": ".COM;.EXE",
            "TEMP": r"C:\Temp",
            "SECRET_TOKEN": "must-not-cross",
        },
        extra={"META_RESEARCH_PROVIDER_OPERATION": r"C:\run\request.json"},
    )

    assert environment == {
        "PATH": r"C:\Windows\System32",
        "SystemRoot": r"C:\Windows",
        "COMSPEC": r"C:\Windows\System32\cmd.exe",
        "PATHEXT": ".COM;.EXE",
        "TEMP": r"C:\Temp",
        "META_RESEARCH_PROVIDER_OPERATION": r"C:\run\request.json",
    }


def test_windows_provider_result_target_is_a_private_file_not_proc(
    tmp_path: Path,
) -> None:
    result_path = tmp_path / ".last-message.provider.tmp"
    argv = [
        "provider",
        "--output-last-message",
        "old-result.json",
        "-",
    ]

    assert provider_result_argv(
        argv,
        platform_name="nt",
        result_path=result_path,
        result_write_fd=17,
        supervisor_process_id=99,
    ) == [
        "provider",
        "--output-last-message",
        str(result_path),
        "-",
    ]


def test_windows_never_started_proof_uses_the_operation_lock_not_proc(
    tmp_path: Path,
) -> None:
    operation = tmp_path / "provider-operations" / "operation" / "phase"
    operation.mkdir(parents=True)
    request_path = operation / "supervisor-request.json"
    invocation_hash = "b" * 64
    request_schema = "meta-research/provider-supervisor-request/v2"
    write_transport_envelope(
        request_path,
        {
            "schema_ref": request_schema,
            "invocation_hash": invocation_hash,
        },
        b"k" * 32,
    )
    old = time.time() - 30
    os.utime(request_path, (old, old))

    assert not supervisor_request_never_started(
        operation,
        key=b"k" * 32,
        invocation_hash=invocation_hash,
        request_schema=request_schema,
        now=old + 30,
        platform_name="nt",
        supervisor_lock_held=lambda path: path.name == "supervisor.lock",
    )
    assert supervisor_request_never_started(
        operation,
        key=b"k" * 32,
        invocation_hash=invocation_hash,
        request_schema=request_schema,
        now=old + 30,
        platform_name="nt",
        supervisor_lock_held=lambda _path: False,
    )


def test_experiment_supervisor_honors_sealed_stop_before_provider_spawn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "experiment"
    operation = workspace / "provider-operations" / "operation"
    operation.mkdir(parents=True)
    key = ensure_experiment_transport_key(workspace)
    invocation_hash = "c" * 64
    request_path = operation / "supervisor-request.json"
    spawned_marker = operation / "provider-was-spawned"
    (operation / "stdin.json").write_text("{}", encoding="utf-8")
    write_experiment_signed(
        request_path,
        {
            "schema_ref": EXPERIMENT_REQUEST_SCHEMA,
            "invocation_hash": invocation_hash,
            "argv": [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(spawned_marker)!r}).touch()"
                ),
            ],
            "wall_timeout_seconds": 1.0,
            "stdout_max_bytes": 4096,
            "stdout_max_records": 32,
            "result_max_bytes": 4096,
            "observation_max_count": 32,
            "telemetry_cadence_seconds": 0.05,
            "stdin_path": str(operation / "stdin.json"),
            "stdout_path": str(operation / "stdout.bin"),
            "observation_path": str(operation / "observations.jsonl"),
            "started_path": str(operation / "provider-started.json"),
            "ready_path": str(operation / "supervisor-ready.json"),
            "receipt_path": str(operation / "supervisor-exit.json"),
        },
        key,
    )
    write_supervisor_stop_request(
        operation / "supervisor-stop.json",
        key=key,
        invocation_hash=invocation_hash,
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "meta_research.experiment_provider_supervisor",
            str(request_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    receipt = read_experiment_signed(operation / "supervisor-exit.json", key)
    assert (completed.returncode, completed.stderr) == (0, "")
    assert receipt["termination_reason"] == "stopped"
    assert not spawned_marker.exists()
    assert not (operation / "provider-started.json").exists()


def test_windows_experiment_waits_for_job_empty_before_terminal_receipt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "experiment-job"
    operation = workspace / "provider-operations" / "operation"
    operation.mkdir(parents=True)
    key = ensure_experiment_transport_key(workspace)
    invocation_hash = "d" * 64
    request_path = operation / "supervisor-request.json"
    receipt_path = operation / "supervisor-exit.json"
    (operation / "stdin.json").write_text("{}", encoding="utf-8")
    write_experiment_signed(
        request_path,
        {
            "schema_ref": EXPERIMENT_REQUEST_SCHEMA,
            "invocation_hash": invocation_hash,
            "argv": ["provider"],
            "wall_timeout_seconds": 1.0,
            "stdout_max_bytes": 4096,
            "stdout_max_records": 32,
            "result_max_bytes": 4096,
            "observation_max_count": 32,
            "telemetry_cadence_seconds": 0.05,
            "stdin_path": str(operation / "stdin.json"),
            "stdout_path": str(operation / "stdout.bin"),
            "observation_path": str(operation / "observations.jsonl"),
            "started_path": str(operation / "provider-started.json"),
            "ready_path": str(operation / "supervisor-ready.json"),
            "receipt_path": str(receipt_path),
        },
        key,
    )
    job = _FakeWindowsJob()
    receipt_absent_before_cancel: list[bool] = []

    def cancel_after_root_exit() -> None:
        if not job.spawned.wait(timeout=1):
            return
        receipt_absent_before_cancel.append(not receipt_path.exists())
        write_supervisor_stop_request(
            operation / "supervisor-stop.json",
            key=key,
            invocation_hash=invocation_hash,
        )

    canceller = threading.Thread(target=cancel_after_root_exit)
    canceller.start()
    try:
        supervise_experiment(
            request_path,
            process_platform=ProviderProcessPlatform(platform_name="nt"),
            provider_job_factory=lambda: job,
        )
    finally:
        canceller.join(timeout=2)

    receipt = read_experiment_signed(receipt_path, key)
    assert receipt_absent_before_cancel == [True]
    assert receipt["termination_reason"] == "stopped"
    assert job.terminated
    assert job.active_process_count() == 0
    assert job.closed


def test_windows_provider_waits_for_job_empty_before_terminal_receipt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider-job"
    _key_path, key = ensure_transport_key(workspace)
    invocation_hash = "e" * 64
    operation = (
        workspace
        / "provider-operations"
        / invocation_hash[:2]
        / invocation_hash
    )
    operation.mkdir(parents=True)
    request_path = operation / "supervisor-request.json"
    prompt_path = operation / "prompt.txt"
    schema_path = operation / "output-schema.json"
    stdout_path = operation / "stdout.jsonl"
    result_path = operation / "last-message.json"
    receipt_path = operation / "supervisor-exit.json"
    prompt_path.write_text("provider input", encoding="utf-8")
    schema_path.write_text("{}", encoding="utf-8")
    paths = {
        "prompt_path": prompt_path,
        "schema_path": schema_path,
        "stdout_path": stdout_path,
        "result_path": result_path,
        "lock_path": operation / "supervisor.lock",
        "ready_path": operation / "supervisor-ready.json",
        "started_path": operation / "provider-started.json",
        "receipt_path": receipt_path,
        "stop_path": operation / "supervisor-stop.json",
    }
    write_supervisor_request(
        request_path,
        {
            "schema_ref": SUPERVISOR_REQUEST_SCHEMA_V2,
            "invocation_hash": invocation_hash,
            "argv": [
                "provider",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
                "-",
            ],
            "timeout_seconds": 1.0,
            "stream_max_bytes": 4096,
            "result_max_bytes": 4096,
            **{name: str(path) for name, path in paths.items()},
        },
        key,
    )
    job = _FakeWindowsJob()
    receipt_absent_before_cancel: list[bool] = []

    def cancel_after_root_exit() -> None:
        if not job.spawned.wait(timeout=1):
            return
        receipt_absent_before_cancel.append(not receipt_path.exists())
        write_supervisor_stop_request(
            paths["stop_path"],
            key=key,
            invocation_hash=invocation_hash,
        )

    canceller = threading.Thread(target=cancel_after_root_exit)
    canceller.start()
    try:
        supervise_provider(
            request_path,
            process_platform=ProviderProcessPlatform(platform_name="nt"),
            provider_job_factory=lambda: job,
        )
    finally:
        canceller.join(timeout=2)

    receipt, _envelope = read_verified_exit_receipt(
        receipt_path,
        key=key,
        invocation_hash=invocation_hash,
        prompt_path=prompt_path,
        schema_path=schema_path,
        stdout_path=stdout_path,
        result_path=result_path,
        expected_schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
    )
    assert receipt_absent_before_cancel == [True]
    assert receipt["termination_reason"] == "stopped"
    assert job.terminated
    assert job.active_process_count() == 0
    assert job.closed
