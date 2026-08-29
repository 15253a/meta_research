from __future__ import annotations

import errno
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from meta_research import cli, daemon
from meta_research.paths import (
    RuntimeState,
    prepare_data_root,
    read_runtime_state,
    write_runtime_state,
)


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


def test_running_state_is_guarded_and_first_pre_capture_signal_is_delegated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = prepare_data_root(tmp_path / "guarded-running-state")
    runtime_closed: list[bool] = []
    runtime = SimpleNamespace(
        feed=SimpleNamespace(current_revision=lambda: 0),
        close=lambda: runtime_closed.append(True),
    )
    published_states: list[RuntimeState] = []
    current_handlers: dict[int, object] = {signal.SIGTERM: signal.SIG_DFL}
    delegated_signals: list[int] = []

    class FakeServer:
        def __init__(self, _config) -> None:
            self.should_exit = False

        def handle_exit(self, signal_number: int, _frame: object) -> None:
            delegated_signals.append(signal_number)
            self.should_exit = True

        def run(self) -> None:
            installed = current_handlers[signal.SIGTERM]
            assert callable(installed)
            installed(signal.SIGTERM, None)
            installed(signal.SIGTERM, None)

    def install_signal(signal_number: int, handler: object) -> object:
        previous = current_handlers.get(signal_number, signal.SIG_DFL)
        current_handlers[signal_number] = handler
        return previous

    def publish_state(_data_root, state: RuntimeState) -> None:
        if state.status == "running":
            assert callable(current_handlers[signal.SIGTERM])
        published_states.append(state)

    monkeypatch.setattr(
        "meta_research.composition.build_production_runtime",
        lambda _data_root: runtime,
    )
    monkeypatch.setattr(
        "meta_research.web.create_app",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr("uvicorn.Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    monkeypatch.setattr("uvicorn.server.HANDLED_SIGNALS", (signal.SIGTERM,))
    monkeypatch.setattr(daemon.signal, "signal", install_signal)
    monkeypatch.setattr(daemon, "write_runtime_state", publish_state)
    monkeypatch.setattr(daemon, "append_daemon_event", lambda *_args: None)

    result = daemon._serve(
        SimpleNamespace(host="127.0.0.1", port=8765),
        data_root,
    )

    assert result == 0
    assert [state.status for state in published_states] == ["running", "stopped"]
    assert delegated_signals == [signal.SIGTERM]
    assert runtime_closed == [True]
    assert current_handlers[signal.SIGTERM] is signal.SIG_DFL


def test_cli_start_gives_readiness_the_remaining_startup_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = prepare_data_root(tmp_path / "slow-readiness")
    state = RuntimeState(
        status="running",
        pid=4312,
        host="127.0.0.1",
        port=8765,
        base_url="http://127.0.0.1:8765",
        version="0.1.0",
        started_at=123.0,
    )
    clock = SimpleNamespace(now=100.0)
    process = SimpleNamespace(pid=state.pid, terminated=False)
    request_timeouts: list[float] = []
    process.poll = lambda: None
    process.terminate = lambda: setattr(process, "terminated", True)

    monkeypatch.setattr(cli, "_running_state", lambda _data_root: None)
    monkeypatch.setattr(cli, "_choose_port", lambda _host, _port: state.port)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(cli, "read_runtime_state", lambda _data_root: state)
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(
        cli.time,
        "sleep",
        lambda seconds: setattr(clock, "now", clock.now + seconds),
    )

    def readiness_request(
        _data_root,
        _state,
        path,
        *,
        method="POST",
        payload=None,
        timeout_seconds=2.0,
    ):
        assert (path, method, payload) == ("/internal/readiness", "GET", None)
        request_timeouts.append(timeout_seconds)
        response_seconds = 2.5
        clock.now += min(timeout_seconds, response_seconds)
        if timeout_seconds < response_seconds:
            raise cli.CliError("daemon control request failed: /internal/readiness")
        return {"status": "ready"}

    monkeypatch.setattr(cli, "_internal_request", readiness_request)

    ready_state, owns_state = cli._ensure_daemon(data_root, state.host, state.port)

    assert ready_state == state
    assert owns_state is True
    assert process.terminated is False
    assert request_timeouts == [pytest.approx(30.0)]


def test_cli_start_still_terminates_an_owned_daemon_that_misses_total_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = prepare_data_root(tmp_path / "readiness-deadline")
    state = RuntimeState(
        status="running",
        pid=4312,
        host="127.0.0.1",
        port=8765,
        base_url="http://127.0.0.1:8765",
        version="0.1.0",
        started_at=123.0,
    )
    clock = SimpleNamespace(now=100.0)
    process = SimpleNamespace(pid=state.pid, terminated=False)
    process.poll = lambda: None
    process.terminate = lambda: setattr(process, "terminated", True)
    request_timeouts: list[float] = []

    monkeypatch.setattr(cli, "_running_state", lambda _data_root: None)
    monkeypatch.setattr(cli, "_choose_port", lambda _host, _port: state.port)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(cli, "read_runtime_state", lambda _data_root: state)
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(
        cli.time,
        "sleep",
        lambda seconds: setattr(clock, "now", clock.now + seconds),
    )

    def readiness_request(
        _data_root,
        _state,
        _path,
        *,
        method="POST",
        payload=None,
        timeout_seconds=2.0,
    ):
        assert (method, payload) == ("GET", None)
        request_timeouts.append(timeout_seconds)
        clock.now += timeout_seconds
        raise cli.CliError("daemon control request failed: /internal/readiness")

    monkeypatch.setattr(cli, "_internal_request", readiness_request)

    with pytest.raises(
        cli.CliError,
        match="daemon did not become ready within 30 seconds",
    ):
        cli._ensure_daemon(data_root, state.host, state.port)

    assert process.terminated is True
    assert request_timeouts == [pytest.approx(30.0)]


def test_internal_request_passes_an_explicit_timeout_to_urllib(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = prepare_data_root(tmp_path / "internal-request-timeout")
    state = RuntimeState(
        status="running",
        pid=4312,
        host="127.0.0.1",
        port=8765,
        base_url="http://127.0.0.1:8765",
        version="0.1.0",
        started_at=123.0,
    )
    observed: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b"{}"

    class Opener:
        def open(self, request, *, timeout):
            observed["url"] = request.full_url
            observed["timeout"] = timeout
            return Response()

    monkeypatch.setattr(cli.urllib.request, "build_opener", lambda *_args: Opener())

    assert cli._internal_request(
        data_root,
        state,
        "/internal/readiness",
        method="GET",
        timeout_seconds=7.25,
    ) == {}
    assert observed == {
        "url": "http://127.0.0.1:8765/internal/readiness",
        "timeout": 7.25,
    }


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


@pytest.mark.skipif(
    os.name != "posix",
    reason="the foreground-daemon exit-code assertion requires POSIX signals",
)
def test_cli_stop_cancels_a_long_lived_sse_before_its_total_deadline(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "streaming-stop-data")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    command = [
        sys.executable,
        "-c",
        "from meta_research.cli import main; raise SystemExit(main())",
    ]
    environment = dict(os.environ)
    environment.update({"TMPDIR": "/dev/shm", "SQLITE_TMPDIR": "/dev/shm"})
    daemon_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "meta_research.daemon",
            "--data-root",
            str(data_root.root),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
    )
    stream_socket: socket.socket | None = None
    daemon_exit_codes: list[int] = []
    daemon_reaped = threading.Event()
    reaper: threading.Thread | None = None
    try:
        ready_deadline = time.monotonic() + 20.0
        while time.monotonic() < ready_deadline:
            state = read_runtime_state(data_root)
            if state is not None and state.status == "running":
                break
            if daemon_process.poll() is not None:
                raise AssertionError(
                    f"daemon exited during startup with {daemon_process.returncode}"
                )
            time.sleep(0.05)
        else:
            raise AssertionError("daemon did not publish its running state")

        issued: subprocess.CompletedProcess[str] | None = None
        session_deadline = time.monotonic() + 20.0
        while time.monotonic() < session_deadline:
            candidate = subprocess.run(
                [
                    *command,
                    "session",
                    "--data-root",
                    str(data_root.root),
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
            )
            if candidate.returncode == 0:
                issued = candidate
                break
            if daemon_process.poll() is not None:
                raise AssertionError(
                    f"daemon exited before readiness with {daemon_process.returncode}"
                )
            time.sleep(0.05)
        assert issued is not None, "daemon control endpoint did not become ready"
        bootstrap_token = str(json.loads(issued.stdout)["bootstrap_token"])
        base_url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
            exchanged = client.post(
                "/auth/bootstrap",
                headers={"Origin": base_url},
                json={"token": bootstrap_token},
            )
            exchanged.raise_for_status()
            cookie_header = "; ".join(
                f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar
            )

        stream_socket = socket.create_connection(("127.0.0.1", port), timeout=5)
        stream_socket.sendall(
            (
                "GET /api/v1/events HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Accept: text/event-stream\r\n"
                f"Cookie: {cookie_header}\r\n"
                "Connection: keep-alive\r\n\r\n"
            ).encode("ascii")
        )
        response = b""
        stream_ready_deadline = time.monotonic() + 5.0
        while time.monotonic() < stream_ready_deadline:
            response += stream_socket.recv(4096)
            if b"\r\n\r\n" in response and (
                b"event:" in response or b": keep-alive" in response
            ):
                break
        assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b"content-type: text/event-stream" in response.lower()

        def reap_daemon() -> None:
            daemon_exit_codes.append(daemon_process.wait())
            daemon_reaped.set()

        reaper = threading.Thread(target=reap_daemon, daemon=True)
        reaper.start()
        stop_started = time.monotonic()
        stopped = subprocess.run(
            [*command, "stop", "--data-root", str(data_root.root), "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
        )
        stop_elapsed = time.monotonic() - stop_started

        assert (stopped.returncode, stopped.stderr) == (0, "")
        assert json.loads(stopped.stdout)["status"] == "stopped"
        assert stop_elapsed < daemon.DAEMON_STOP_DEADLINE_SECONDS
        assert daemon_reaped.wait(timeout=2)
        assert daemon_exit_codes == [0]

        status = subprocess.run(
            [*command, "status", "--data-root", str(data_root.root), "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
        assert json.loads(status.stdout)["status"] == "stopped"
        durable_state = read_runtime_state(data_root)
        assert durable_state is not None
        assert durable_state.status == "stopped"
        assert durable_state.stopped_at is not None

        stream_socket.settimeout(0.5)
        connection_cancelled = False
        cancellation_deadline = time.monotonic() + 2.0
        while time.monotonic() < cancellation_deadline:
            try:
                chunk = stream_socket.recv(4096)
            except socket.timeout:
                continue
            except ConnectionResetError:
                connection_cancelled = True
                break
            if not chunk:
                connection_cancelled = True
                break
        assert connection_cancelled
    finally:
        if stream_socket is not None:
            stream_socket.close()
        if reaper is None and daemon_process.poll() is None:
            daemon_process.terminate()
            try:
                daemon_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Failure-only containment for the pre-fix regression: closing the
                # test stream above normally lets Uvicorn finish after SIGTERM.
                daemon_process.kill()
                daemon_process.wait(timeout=5)
        elif reaper is not None and not daemon_reaped.is_set():
            daemon_process.terminate()
            if not daemon_reaped.wait(timeout=10):
                daemon_process.kill()
                assert daemon_reaped.wait(timeout=5)
        if reaper is not None:
            reaper.join(timeout=1)
