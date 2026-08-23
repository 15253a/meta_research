from __future__ import annotations

import argparse
import html
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from meta_research import __version__
from meta_research.paths import (
    DataRoot,
    DataRootError,
    RuntimeState,
    default_data_root,
    prepare_data_root,
    read_control_key,
    read_runtime_state,
)


class CliError(RuntimeError):
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meta-research")
    commands = parser.add_subparsers(dest="command", required=True)

    version_parser = commands.add_parser("version", help="Show the installed release")
    version_parser.add_argument("--json", action="store_true", dest="as_json")

    init_parser = commands.add_parser("init", help="Create an isolated vNext data root")
    _add_data_root(init_parser)
    init_parser.add_argument("--json", action="store_true", dest="as_json")

    start_parser = commands.add_parser("start", help="Start the detached local daemon")
    _add_data_root(start_parser)
    _add_listener(start_parser)
    start_parser.add_argument("--json", action="store_true", dest="as_json")

    status_parser = commands.add_parser("status", help="Inspect daemon status")
    _add_data_root(status_parser)
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    doctor_parser = commands.add_parser(
        "doctor", help="Inspect locked Harness and MCP capabilities"
    )
    _add_data_root(doctor_parser)
    doctor_parser.add_argument("--json", action="store_true", dest="as_json")

    session_parser = commands.add_parser(
        "session", help="Issue a one-use browser bootstrap token"
    )
    _add_data_root(session_parser)
    session_parser.add_argument("--json", action="store_true", dest="as_json")

    launch_parser = commands.add_parser(
        "launch", help="Open an authenticated loopback Web session"
    )
    _add_data_root(launch_parser)
    _add_listener(launch_parser)
    launch_parser.add_argument("--no-browser", action="store_true")
    launch_parser.add_argument("--json", action="store_true", dest="as_json")

    stop_parser = commands.add_parser("stop", help="Stop the local daemon")
    _add_data_root(stop_parser)
    stop_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "version":
            return _emit(
                {"product": "meta-research-vnext", "version": __version__},
                args.as_json,
                human=__version__,
            )

        data_root = prepare_data_root(args.data_root)
        if args.command == "init":
            return _emit(
                {"status": "initialized", "data_root": str(data_root.root)},
                args.as_json,
                human=f"Initialized {data_root.root}",
            )
        if args.command == "start":
            state, started = _ensure_daemon(data_root, args.host, args.port)
            token = _internal_request(data_root, state, "/internal/bootstrap-token")
            result = _public_runtime_result(data_root, state)
            result.update(
                {
                    "status": "started" if started else "already_running",
                    "bootstrap_token": token["bootstrap_token"],
                }
            )
            return _emit(result, args.as_json, human=_start_human(result))
        if args.command == "status":
            status = _daemon_status(data_root)
            return _emit(
                status,
                args.as_json,
                human=f"Daemon is {status['status']}",
            )
        if args.command == "doctor":
            state = _require_running(data_root)
            result = _internal_request(
                data_root, state, "/internal/doctor", method="GET"
            )
            return _emit(
                result,
                args.as_json,
                human=(
                    f"Harness gateway is {result['status']} at "
                    f"{state.base_url}/mcp"
                ),
            )
        if args.command == "session":
            state = _require_running(data_root)
            token = _internal_request(data_root, state, "/internal/bootstrap-token")
            result = _public_runtime_result(data_root, state)
            result.update(token)
            result["status"] = "issued"
            return _emit(
                result,
                args.as_json,
                human=f"One-use bootstrap token: {token['bootstrap_token']}",
            )
        if args.command == "launch":
            state, _started = _ensure_daemon(data_root, args.host, args.port)
            issued = _internal_request(data_root, state, "/internal/browser-grant")
            browser_grant = str(issued["browser_grant"])
            launch_document = _create_launch_document(
                data_root, state.base_url, browser_grant
            )
            result = _public_runtime_result(data_root, state)
            result["status"] = "launch_ready"
            result["browser_url"] = launch_document.as_uri()
            result["target_url"] = state.base_url
            if args.no_browser:
                return _emit(
                    result,
                    args.as_json,
                    human=f"Open {result['browser_url']}",
                )
            try:
                if not webbrowser.open(launch_document.as_uri()):
                    raise CliError("the system browser could not be opened")
                _wait_for_browser_grant(data_root, state, browser_grant)
                return _emit(
                    result,
                    args.as_json,
                    human=f"Opened authenticated Web at {result['target_url']}",
                )
            finally:
                launch_document.unlink(missing_ok=True)
        if args.command == "stop":
            result = _stop_daemon(data_root)
            return _emit(result, args.as_json, human=f"Daemon is {result['status']}")
    except (CliError, DataRootError) as error:
        if getattr(args, "as_json", False):
            print(
                json.dumps(
                    {"status": "error", "reason": str(error)}, separators=(",", ":")
                )
            )
        else:
            print(f"meta-research: {error}", file=sys.stderr)
        return 1

    raise AssertionError(f"unhandled command: {args.command}")


def _add_data_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-root", type=Path, default=default_data_root())


def _add_listener(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "::1"))
    parser.add_argument("--port", type=int, default=8765)


def _ensure_daemon(
    data_root: DataRoot, host: str, requested_port: int
) -> tuple[RuntimeState, bool]:
    current = _running_state(data_root)
    if current is not None:
        return current, False
    port = _choose_port(host, requested_port)
    command = [
        sys.executable,
        "-m",
        "meta_research.daemon",
        "--data-root",
        str(data_root.root),
        "--host",
        host,
        "--port",
        str(port),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CliError("daemon exited during startup; inspect the local daemon log")
        state = read_runtime_state(data_root)
        if state and state.pid == process.pid and state.status == "running":
            try:
                _internal_request(data_root, state, "/internal/readiness", method="GET")
                return state, True
            except CliError:
                pass
        time.sleep(0.05)
    process.terminate()
    raise CliError("daemon did not become ready within 15 seconds")


def _choose_port(host: str, requested: int) -> int:
    if requested < 0 or requested > 65535:
        raise CliError("port must be between 0 and 65535")
    if requested:
        return requested
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.bind((host, 0))
        return int(listener.getsockname()[1])


def _daemon_status(data_root: DataRoot) -> dict[str, Any]:
    state = _running_state(data_root)
    if state is None:
        return {"status": "stopped", "data_root": str(data_root.root)}
    result = state.as_dict()
    result["data_root"] = str(data_root.root)
    return result


def _running_state(data_root: DataRoot) -> RuntimeState | None:
    state = read_runtime_state(data_root)
    if state is None or state.status != "running":
        return None
    return state if _process_matches(state.pid, data_root) else None


def _process_matches(pid: int, data_root: DataRoot) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    command_line = Path(f"/proc/{pid}/cmdline")
    try:
        arguments = command_line.read_bytes().split(b"\0")
    except OSError:
        return False
    decoded = [argument.decode("utf-8", errors="replace") for argument in arguments]
    return "meta_research.daemon" in decoded and str(data_root.root) in decoded


def _require_running(data_root: DataRoot) -> RuntimeState:
    state = _running_state(data_root)
    if state is None:
        raise CliError("daemon is not running")
    return state


def _stop_daemon(data_root: DataRoot) -> dict[str, object]:
    state = _running_state(data_root)
    if state is None:
        return {"status": "stopped", "data_root": str(data_root.root)}
    pid = state.pid
    if not _process_matches(pid, data_root):
        raise CliError("refusing to signal a process that is not this data root's daemon")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not _process_matches(pid, data_root):
            return {"status": "stopped", "data_root": str(data_root.root)}
        time.sleep(0.05)
    raise CliError("daemon did not stop within 15 seconds")


def _internal_request(
    data_root: DataRoot,
    state: RuntimeState,
    path: str,
    *,
    method: str = "POST",
    payload: dict[str, object] | None = None,
) -> dict[str, Any]:
    request_body = None
    if method != "GET":
        request_body = json.dumps(payload or {}, separators=(",", ":")).encode(
            "utf-8"
        )
    request = urllib.request.Request(
        f"{state.base_url}{path}",
        data=request_body,
        method=method,
        headers={
            "X-Meta-Research-Control": read_control_key(data_root),
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=2) as response:
            body = response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise CliError(f"daemon control request failed: {path}") from error
    if not body:
        return {}
    value = json.loads(body)
    if not isinstance(value, dict):
        raise CliError(f"daemon returned an invalid control response: {path}")
    return value


def _public_runtime_result(data_root: DataRoot, state: RuntimeState) -> dict[str, object]:
    base_url = state.base_url
    return {
        "data_root": str(data_root.root),
        "pid": state.pid,
        "base_url": base_url,
        "web_url": base_url,
    }


def _emit(value: dict[str, object], as_json: bool, *, human: str) -> int:
    print(json.dumps(value, separators=(",", ":")) if as_json else human)
    return 0


def _start_human(result: dict[str, object]) -> str:
    return (
        f"Daemon {result['status']} at {result['base_url']}\n"
        f"One-use bootstrap token: {result['bootstrap_token']}"
    )


def _create_launch_document(
    data_root: DataRoot, base_url: str, browser_grant: str
) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix="browser-launch-", suffix=".html", dir=data_root.run
    )
    path = Path(raw_path)
    action = html.escape(f"{base_url}/auth/launch", quote=True)
    escaped_grant = html.escape(browser_grant, quote=True)
    document = f"""<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Opening Meta-research</title></head>
  <body>
    <form id="launch" method="post" action="{action}">
      <input type="hidden" name="token" value="{escaped_grant}">
      <button type="submit">Open Meta-research</button>
    </form>
    <script>document.getElementById("launch").requestSubmit()</script>
  </body>
</html>
"""
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(document)
    path.chmod(0o600)
    return path


def _wait_for_browser_grant(
    data_root: DataRoot, state: RuntimeState, browser_grant: str
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = _internal_request(
            data_root,
            state,
            "/internal/browser-grant-status",
            payload={"token": browser_grant},
        )
        if status.get("consumed") is True:
            return
        time.sleep(0.05)
    raise CliError("browser did not complete authentication within 30 seconds")
