from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from meta_research import cli, daemon
from meta_research.paths import RuntimeState, prepare_data_root, write_runtime_state


def test_daemon_lock_seam_imports_when_fcntl_is_unavailable() -> None:
    script = """
import builtins
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "fcntl":
        raise ImportError("fcntl deliberately unavailable")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from meta_research.daemon import DaemonFileLock
assert DaemonFileLock.__name__ == "DaemonFileLock"
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert (completed.returncode, completed.stderr) == (0, "")


def test_posix_daemon_lock_rejects_a_second_instance(tmp_path: Path) -> None:
    path = tmp_path / "daemon.lock"
    first = daemon.DaemonFileLock(path, platform_name="posix")
    second = daemon.DaemonFileLock(path, platform_name="posix")

    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()

    assert second.acquire() is True
    second.release()


def test_windows_daemon_lock_is_testable_without_importing_msvcrt(
    tmp_path: Path,
) -> None:
    calls: list[tuple[int, bool]] = []

    def lock_operation(descriptor: int, acquire: bool) -> None:
        assert os.fstat(descriptor).st_size == 1
        calls.append((descriptor, acquire))

    lock = daemon.DaemonFileLock(
        tmp_path / "daemon.lock",
        platform_name="nt",
        lock_operation=lock_operation,
    )

    assert lock.acquire() is True
    lock.release()

    assert [acquire for _descriptor, acquire in calls] == [True, False]


def test_windows_daemon_lock_fails_closed_when_the_byte_is_already_locked(
    tmp_path: Path,
) -> None:
    def busy_lock(_descriptor: int, acquire: bool) -> None:
        if acquire:
            raise OSError(errno.EACCES, "byte already locked")

    lock = daemon.DaemonFileLock(
        tmp_path / "daemon.lock",
        platform_name="nt",
        lock_operation=busy_lock,
    )

    assert lock.acquire() is False


def test_windows_pid_liveness_does_not_call_the_posix_signal_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_posix_probe(_pid: int, _signal: int) -> None:
        raise AssertionError("Windows PID liveness must not use os.kill(pid, 0)")

    monkeypatch.setattr(cli.os, "kill", reject_posix_probe)

    assert cli.pid_is_alive(
        4312,
        platform_name="nt",
        windows_probe=lambda pid: pid == 4312,
    )


def test_windows_pid_identity_uses_liveness_and_authenticated_runtime_attestation(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "data")
    inspected: list[int] = []

    def pid_liveness(pid: int) -> bool:
        inspected.append(pid)
        return True

    def reject_command_line(_pid: int) -> None:
        raise AssertionError("Windows identity must not read /proc")

    assert (
        cli.daemon_process_matches(
            4312,
            data_root,
            platform_name="nt",
            pid_liveness=pid_liveness,
            command_line_reader=reject_command_line,
        )
        is False
    )
    assert cli.daemon_process_matches(
        4312,
        data_root,
        platform_name="nt",
        pid_liveness=pid_liveness,
        command_line_reader=reject_command_line,
        runtime_attestor=lambda: True,
    )
    assert inspected == [4312, 4312]


def test_windows_running_state_fails_closed_without_control_attestation(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "data")
    state = RuntimeState(
        status="running",
        pid=4312,
        host="127.0.0.1",
        port=8765,
        base_url="http://127.0.0.1:8765",
        version="0.1.0",
        started_at=123.0,
    )
    write_runtime_state(data_root, state)

    assert (
        cli._running_state(
            data_root,
            platform_name="nt",
            pid_liveness=lambda _pid: True,
            runtime_attestor=lambda: False,
        )
        is None
    )
    assert (
        cli._running_state(
            data_root,
            platform_name="nt",
            pid_liveness=lambda _pid: True,
            runtime_attestor=lambda: True,
        )
        == state
    )


def test_windows_daemon_spawn_is_detached_from_the_parent_terminal() -> None:
    options = cli.daemon_spawn_options(
        platform_name="nt",
        create_new_process_group=0x200,
        detached_process=0x8,
    )

    assert options == {"close_fds": True, "creationflags": 0x208}
    assert "start_new_session" not in options


def test_concurrent_cli_start_adopts_the_daemon_lock_winner(tmp_path: Path) -> None:
    data_root = tmp_path / "concurrent-data"
    command = [
        sys.executable,
        "-c",
        "from meta_research.cli import main; raise SystemExit(main())",
    ]
    environment = dict(os.environ)
    environment.update({"TMPDIR": "/dev/shm", "SQLITE_TMPDIR": "/dev/shm"})
    initialized = subprocess.run(
        [*command, "init", "--data-root", str(data_root), "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    assert json.loads(initialized.stdout)["status"] == "initialized"

    start_command = [
        *command,
        "start",
        "--data-root",
        str(data_root),
        "--port",
        "0",
        "--json",
    ]
    first = subprocess.Popen(
        start_command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    second = subprocess.Popen(
        start_command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        first_stdout, first_stderr = first.communicate(timeout=30)
        second_stdout, second_stderr = second.communicate(timeout=30)
        assert (first.returncode, first_stderr) == (0, "")
        assert (second.returncode, second_stderr) == (0, "")
        results = [json.loads(first_stdout), json.loads(second_stdout)]
        assert sorted(result["status"] for result in results) == [
            "already_running",
            "started",
        ]
        assert results[0]["pid"] == results[1]["pid"]
    finally:
        for process in (first, second):
            if process.poll() is None:
                process.terminate()
        subprocess.run(
            [*command, "stop", "--data-root", str(data_root), "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
        )
