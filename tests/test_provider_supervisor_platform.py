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
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2,
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ProviderProcessPlatform,
    ProviderSupervisorError,
    SupervisorFileLock,
    WindowsProviderJob,
    ensure_transport_key,
    provider_result_argv,
    read_verified_exit_receipt,
    request_supervisor_stop,
    supervise as supervise_provider,
    supervisor_request_never_started,
    supervisor_stop_requested,
    write_supervisor_request,
    write_supervisor_stop_request,
    write_transport_envelope,
    write_exit_receipt,
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


def test_provider_supervisor_imports_without_fcntl() -> None:
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
assert SupervisorFileLock.__name__ == "SupervisorFileLock"
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
                for name in ("prompt.txt", "output-schema.json", "stdout.jsonl", "last-message.json"):
                    (operation / name).write_text("")
                write_exit_receipt(operation / "supervisor-exit.json", key=key,
                    invocation_hash=invocation_hash, prompt_path=operation / "prompt.txt",
                    schema_path=operation / "output-schema.json", stdout_path=operation / "stdout.jsonl",
                    result_path=operation / "last-message.json", returncode=1, input_bytes=0,
                    termination_reason="stopped", schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
                    process_tree={"schema_ref": "meta-research/provider-process-tree/v1",
                        "state": "closed", "mechanism": "windows-job",
                        "boot_identity_hash": None, "host_identity_hash": "a" * 64})
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    held = True
    class TestLock:
        def __init__(self, path, *, platform_name):
            assert path.name == "supervisor.lock" and platform_name == "nt"
        def acquire(self, *, blocking):
            assert blocking is False
            return not held
        def release(self):
            pass
    monkeypatch.setattr("meta_research.provider_supervisor.SupervisorFileLock", TestLock)

    assert not supervisor_request_never_started(
        operation,
        key=b"k" * 32,
        invocation_hash=invocation_hash,
        request_schema=request_schema,
        platform_name="nt",
    )
    held = False
    assert supervisor_request_never_started(
        operation,
        key=b"k" * 32,
        invocation_hash=invocation_hash,
        request_schema=request_schema,
        platform_name="nt",
    )


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


def test_linux_machine_id_fallback_chain_and_32_hex_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import json
    import platform as system_platform

    boot_file = tmp_path / "boot_id"
    boot_file.write_text("11111111-2222-3333-4444-555555555555\n", encoding="utf-8")
    etc_machine = tmp_path / "etc-machine-id"
    dbus_machine = tmp_path / "dbus-machine-id"
    run_machine = tmp_path / "run-machine-id"

    monkeypatch.setattr(provider_supervisor_module, "_current_platform", lambda: "linux")
    monkeypatch.setattr(provider_supervisor_module, "_LINUX_BOOT_ID_PATH", boot_file)
    monkeypatch.setattr(
        provider_supervisor_module,
        "_LINUX_MACHINE_ID_PATHS",
        (etc_machine, dbus_machine, run_machine),
    )

    # 1. Empty 0-byte /etc/machine-id falls back to valid 32-hex /var/lib/dbus/machine-id.
    etc_machine.write_bytes(b"")
    dbus_id = "0123456789abcdef0123456789abcdef"
    dbus_machine.write_text(dbus_id + "\n", encoding="utf-8")
    identity = provider_supervisor_module.supervisor_host_identity()
    expected_host_hash = hashlib.sha256(
        json.dumps(
            {"machine_id": dbus_id, "node": system_platform.node()},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert identity["host_identity_hash"] == expected_host_hash
    assert isinstance(identity["boot_identity_hash"], str) and len(identity["boot_identity_hash"]) == 64

    # 2. Invalid /etc/machine-id and missing dbus fall back to /run/machine-id
    # while preserving the raw 32-hex casing for host_identity_hash stability.
    etc_machine.write_text("uninitialized\n", encoding="utf-8")
    dbus_machine.unlink()
    run_id = "ABCDEF0123456789abcdef0123456789"
    run_machine.write_text(run_id + "\n", encoding="utf-8")
    identity_run = provider_supervisor_module.supervisor_host_identity()
    expected_run_hash = hashlib.sha256(
        json.dumps(
            {"machine_id": run_id, "node": system_platform.node()},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert identity_run["host_identity_hash"] == expected_run_hash

    # 3. All machine-id sources missing/invalid -> fail closed.
    run_machine.write_text("not-32-hex\n", encoding="utf-8")
    with pytest.raises(
        provider_supervisor_module.ProviderSupervisorError,
        match="^provider_host_identity_unavailable$",
    ):
        provider_supervisor_module.supervisor_host_identity()


def test_supervisor_missing_machine_id_fails_closed_cleanly_with_structured_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import json
    import sys as _sys
    import meta_research.streaming_provider_supervisor as streaming_module
    from meta_research.provider_supervisor import (
        SUPERVISOR_REQUEST_SCHEMA_V2,
        ensure_transport_key,
        write_supervisor_request,
    )

    _, key = ensure_transport_key(tmp_path)
    invocation_hash = "c" * 64
    operation = tmp_path / "provider-operations" / invocation_hash[:2] / invocation_hash
    operation.mkdir(parents=True)
    prompt_path = operation / "prompt.txt"
    schema_path = operation / "output-schema.json"
    stdout_path = operation / "stdout.jsonl"
    result_path = operation / "last-message.json"
    stderr_path = operation / "provider-stderr.log"
    ready_path = operation / "supervisor-ready.json"
    started_path = operation / "provider-started.json"
    receipt_path = operation / "supervisor-exit.json"
    stop_path = operation / "supervisor-stop.json"
    lock_path = operation / "supervisor.lock"
    prompt_path.write_text("bounded prompt", encoding="utf-8")
    schema_path.write_text("{}", encoding="utf-8")
    request_path = operation / "supervisor-request.json"
    write_supervisor_request(
        request_path,
        {
            "schema_ref": SUPERVISOR_REQUEST_SCHEMA_V2,
            "invocation_hash": invocation_hash,
            "argv": [
                _sys.executable, "-c", "print('ok')",
                "--output-schema", str(schema_path),
                "--output-last-message", str(result_path), "-",
            ],
            "timeout_seconds": 5.0,
            "stream_max_bytes": 4096,
            "result_max_bytes": 4096,
            "prompt_path": str(prompt_path),
            "schema_path": str(schema_path),
            "stdout_path": str(stdout_path),
            "result_path": str(result_path),
            "lock_path": str(lock_path),
            "ready_path": str(ready_path),
            "started_path": str(started_path),
            "receipt_path": str(receipt_path),
            "stop_path": str(stop_path),
        },
        key,
    )

    boot_file = tmp_path / "boot_id"
    boot_file.write_text("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n", encoding="utf-8")
    missing_a = tmp_path / "empty-etc-machine-id"
    missing_a.write_bytes(b"")
    missing_b = tmp_path / "missing-dbus-machine-id"
    missing_c = tmp_path / "missing-run-machine-id"

    monkeypatch.setattr(provider_supervisor_module, "_current_platform", lambda: "linux")
    monkeypatch.setattr(provider_supervisor_module, "_LINUX_BOOT_ID_PATH", boot_file)
    monkeypatch.setattr(
        provider_supervisor_module,
        "_LINUX_MACHINE_ID_PATHS",
        (missing_a, missing_b, missing_c),
    )

    for entrypoint in (provider_supervisor_module.main, streaming_module.main):
        rc = entrypoint([str(request_path)])
        captured = capsys.readouterr()
        assert rc == 70
        assert captured.out == ""
        err = json.loads(captured.err)
        assert err == {
            "schema_ref": "meta-research/provider-supervisor-error/v1",
            "exit_code": 70,
            "error_type": "ProviderSupervisorError",
            "error_code": "provider_host_identity_unavailable",
        }
        assert str(tmp_path) not in captured.err
        # Zero dirty spool files left behind so retries do not hit spool_invalid.
        assert not stdout_path.exists()
        assert not (operation / ".last-message.supervisor.tmp").exists()
        assert not stderr_path.exists()
        assert not ready_path.exists()
        assert not result_path.exists()
