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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from meta_research import __version__
from meta_research.paths import (
    DataRoot,
    DataRootError,
    RuntimeState,
    prepare_data_root,
    read_control_key,
    read_runtime_state,
)
from meta_research.provider_supervisor import protected_subprocess_environment


_DOCTOR_OWNER_SCOPES = frozenset({"agent_runtime", "human_collaboration"})
_DOCTOR_EFFECT_KINDS = frozenset(
    {
        "provider_unit",
        "drafting_claim",
        "acquisition",
        "harness_root",
        "harness_probe",
        "runtime_reconciliation",
    }
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

    conformance_parser = commands.add_parser(
        "conformance", help="Run the fixed Codex and Claude Harness contract"
    )
    conformance_commands = conformance_parser.add_subparsers(
        dest="conformance_command", required=True
    )
    conformance_start_parser = conformance_commands.add_parser(
        "start", help="Start the full durable Harness conformance matrix"
    )
    _add_data_root(conformance_start_parser)
    conformance_start_parser.add_argument("--codex-model", required=True)
    conformance_start_parser.add_argument(
        "--codex-auth-profile",
        default="harness-profile:codex-default",
    )
    conformance_start_parser.add_argument("--claude-model", required=True)
    conformance_start_parser.add_argument(
        "--claude-auth-profile",
        default="harness-profile:claude-default",
    )
    conformance_start_parser.add_argument(
        "--json", action="store_true", dest="as_json"
    )

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

        data_root = prepare_data_root(_explicit_data_root(args.data_root))
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
                human=_doctor_human(result),
            )
        if args.command == "conformance":
            state = _require_running(data_root)
            result = _internal_request(
                data_root,
                state,
                "/internal/harness-conformance",
                payload={
                    "codex_model_ref": args.codex_model,
                    "codex_auth_profile_ref": args.codex_auth_profile,
                    "claude_model_ref": args.claude_model,
                    "claude_auth_profile_ref": args.claude_auth_profile,
                },
            )
            return _emit(
                result,
                args.as_json,
                human=(
                    "Started full Harness conformance "
                    f"{result['conformance_ref']}"
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
    parser.add_argument(
        "--data-root",
        type=Path,
        help="absolute deployment data root (or set META_RESEARCH_DATA_ROOT)",
    )


def _explicit_data_root(argument: Path | None) -> Path:
    selected = argument
    if selected is None:
        configured = os.environ.get("META_RESEARCH_DATA_ROOT")
        if configured:
            selected = Path(configured)
    if selected is None:
        raise CliError(
            "an explicit data root is required; pass --data-root or set "
            "META_RESEARCH_DATA_ROOT"
        )
    expanded = selected.expanduser()
    if not expanded.is_absolute():
        raise CliError("the data root must be an absolute deployment path")
    return expanded


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
        env=_daemon_environment(data_root),
        **daemon_spawn_options(),
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code not in {None, 2}:
            raise CliError("daemon exited during startup; inspect the local daemon log")
        state = read_runtime_state(data_root)
        owns_state = state is not None and state.pid == process.pid
        can_adopt_winner = exit_code == 2
        if (
            state is not None
            and state.status == "running"
            and (owns_state or can_adopt_winner)
        ):
            try:
                _internal_request(data_root, state, "/internal/readiness", method="GET")
                return state, owns_state
            except CliError:
                pass
        time.sleep(0.05)
    if process.poll() is None:
        process.terminate()
    if process.poll() == 2:
        raise CliError(
            "another daemon acquired the data-root lock but did not become ready"
        )
    raise CliError("daemon did not become ready within 15 seconds")


def _daemon_environment(data_root: DataRoot) -> dict[str, str]:
    return protected_subprocess_environment(
        protected=data_root.codex_environment,
    )


def daemon_spawn_options(
    *,
    platform_name: str | None = None,
    create_new_process_group: int | None = None,
    detached_process: int | None = None,
) -> dict[str, Any]:
    """Return terminal-independent Popen options for the host platform."""

    selected_platform = platform_name or os.name
    if selected_platform == "posix":
        return {"close_fds": True, "start_new_session": True}
    if selected_platform == "nt":
        process_group = (
            create_new_process_group
            if create_new_process_group is not None
            else getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        detached = (
            detached_process
            if detached_process is not None
            else getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
        return {
            "close_fds": True,
            "creationflags": process_group | detached,
        }
    raise CliError(f"daemon detachment is unsupported on platform {selected_platform}")


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


def _running_state(
    data_root: DataRoot,
    *,
    platform_name: str | None = None,
    pid_liveness: Callable[[int], bool] | None = None,
    runtime_attestor: Callable[[], bool] | None = None,
) -> RuntimeState | None:
    state = read_runtime_state(data_root)
    if state is None or state.status != "running":
        return None
    attestor = runtime_attestor or (
        lambda: _runtime_is_attested(data_root, state)
    )
    return (
        state
        if daemon_process_matches(
            state.pid,
            data_root,
            platform_name=platform_name,
            pid_liveness=pid_liveness,
            runtime_attestor=attestor,
        )
        else None
    )


def pid_is_alive(
    pid: int,
    *,
    platform_name: str | None = None,
    windows_probe: Callable[[int], bool] | None = None,
) -> bool:
    """Probe PID liveness without importing a platform-only module eagerly."""

    if pid <= 0:
        return False
    selected_platform = platform_name or os.name
    if selected_platform == "nt":
        probe = windows_probe or _windows_pid_is_alive
        return probe(pid)
    if selected_platform != "posix":
        return False
    try:
        os.kill(pid, 0)
    except (OverflowError, ValueError):
        return False
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def daemon_process_matches(
    pid: int,
    data_root: DataRoot,
    *,
    platform_name: str | None = None,
    pid_liveness: Callable[[int], bool] | None = None,
    command_line_reader: Callable[[int], Sequence[str] | None] | None = None,
    runtime_attestor: Callable[[], bool] | None = None,
) -> bool:
    """Confirm daemon identity before status reporting or process signalling."""

    selected_platform = platform_name or os.name
    is_alive = pid_liveness or (
        lambda candidate: pid_is_alive(
            candidate,
            platform_name=selected_platform,
        )
    )
    if not is_alive(pid):
        return False
    if selected_platform == "posix":
        reader = command_line_reader or _read_proc_command_line
        arguments = reader(pid)
        if arguments is not None:
            return (
                "meta_research.daemon" in arguments
                and str(data_root.root) in arguments
            )
    elif selected_platform != "nt":
        return False
    return runtime_attestor() if runtime_attestor is not None else False


def _read_proc_command_line(pid: int) -> tuple[str, ...] | None:
    command_line = Path(f"/proc/{pid}/cmdline")
    try:
        arguments = command_line.read_bytes().split(b"\0")
    except OSError:
        return None
    return tuple(
        argument.decode("utf-8", errors="replace")
        for argument in arguments
        if argument
    )


def _windows_pid_is_alive(pid: int) -> bool:
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

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _runtime_is_attested(data_root: DataRoot, state: RuntimeState) -> bool:
    try:
        _internal_request(data_root, state, "/internal/readiness", method="GET")
    except (CliError, OSError, ValueError):
        return False
    return True


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
    if not daemon_process_matches(
        pid,
        data_root,
        runtime_attestor=lambda: _runtime_is_attested(data_root, state),
    ):
        raise CliError("refusing to signal a process that is not this data root's daemon")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
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


def _doctor_human(result: dict[str, object]) -> str:
    """Render only bounded, non-identifying runtime diagnostics."""

    runtime = _doctor_mapping(result.get("runtime_protection"))
    inhibitor = _doctor_mapping(runtime.get("inhibitor"))
    capability = _doctor_mapping(inhibitor.get("capability"))
    capability_probed_at = _doctor_nonnegative_number(
        capability.get("probed_at")
    )
    capability_probed_at_text = (
        "unknown"
        if capability_probed_at is None
        else str(round(capability_probed_at))
    )
    current_owners = _doctor_current_owners(runtime.get("responsibilities"))
    durable_waiting = runtime.get("durable_waiting")
    waiting_items = durable_waiting if isinstance(durable_waiting, list) else []
    waiting_count = _doctor_count(runtime.get("durable_waiting_count"))
    interruptions = runtime.get("interruptions")
    interruption_items = interruptions if isinstance(interruptions, list) else []
    interruption_count = _doctor_count(runtime.get("interruption_count"))
    kinds = _doctor_item_atoms(interruption_items, "kind", empty="none")
    waiting_reasons = _doctor_item_reason_atoms(waiting_items, empty="none")
    interruption_reasons = _doctor_item_reason_atoms(
        interruption_items,
        empty="none",
    )
    reconciliation = _doctor_item_atoms(
        interruption_items,
        "reconciliation_status",
        empty="reconciled",
    )
    log = _doctor_mapping(runtime.get("log"))
    telemetry = _doctor_mapping(runtime.get("telemetry"))
    log_age = _doctor_nonnegative_number(log.get("age_seconds"))
    log_age_suffix = (
        "" if log_age is None else f" age_seconds={round(log_age)}"
    )
    return "\n".join(
        (
            f"Harness gateway: {_doctor_atom(result.get('status'))}",
            f"Runtime protection: {_doctor_atom(runtime.get('status'))}",
            "Power inhibitor: "
            f"backend={_doctor_atom(inhibitor.get('backend'))} "
            f"status={_doctor_atom(inhibitor.get('status'))} "
            f"scope={_doctor_atom(inhibitor.get('scope'))} "
            f"active_count={_doctor_count(inhibitor.get('active_count'))} "
            f"reason={_doctor_reason(inhibitor.get('reason'))}",
            "Capability probe: "
            f"status={_doctor_atom(capability.get('status'))} "
            f"backend={_doctor_atom(capability.get('backend'))} "
            f"scope={_doctor_atom(capability.get('scope'))} "
            f"reason={_doctor_reason(capability.get('reason'))} "
            f"probed_at={capability_probed_at_text}",
            f"Current owners: {current_owners}",
            f"Durable waiting: count={waiting_count} reasons={waiting_reasons}",
            "Interruption: "
            f"count={interruption_count} kinds={kinds} "
            f"reasons={interruption_reasons}",
            f"Reconciliation: {reconciliation}",
            f"Log freshness: {_doctor_atom(log.get('status'))}{log_age_suffix}",
            f"Telemetry mode: {_doctor_atom(telemetry.get('mode'))}",
        )
    )


def _doctor_mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _doctor_atom(value: object, *, maximum: int = 64) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        return "unknown"
    if not value[0].isascii() or not value[0].isalnum():
        return "unknown"
    if not all(
        character.isascii()
        and (character.isalnum() or character in {"_", "-", "."})
        for character in value
    ):
        return "unknown"
    return value


def _doctor_reason(value: object) -> str:
    if value is None:
        return "none"
    reason = _doctor_mapping(value)
    if not reason:
        return "unknown"
    return _doctor_atom(reason.get("code"), maximum=96)


def _doctor_count(value: object) -> str:
    return (
        str(value)
        if type(value) is int and 0 <= value <= 2_147_483_647
        else "unknown"
    )


def _doctor_nonnegative_number(value: object) -> int | float | None:
    if type(value) not in {int, float}:
        return None
    return value if 0 <= value <= 2_147_483_647 else None


def _doctor_item_atoms(
    items: list[object],
    field: str,
    *,
    empty: str,
) -> str:
    if not items:
        return empty
    values = {
        _doctor_atom(item.get(field))
        for item in items
        if isinstance(item, dict)
    }
    return ",".join(sorted(values)) if values else "unknown"


def _doctor_item_reason_atoms(items: list[object], *, empty: str) -> str:
    if not items:
        return empty
    values = {
        _doctor_reason(item.get("reason"))
        for item in items
        if isinstance(item, dict)
    }
    return ",".join(sorted(values)) if values else "unknown"


def _doctor_current_owners(value: object) -> str:
    if not isinstance(value, list):
        return "unknown"
    counts: dict[tuple[str, str], int] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        owner_scope = item.get("owner_scope")
        effect_kind = item.get("effect_kind")
        if (
            not isinstance(owner_scope, str)
            or not isinstance(effect_kind, str)
            or owner_scope not in _DOCTOR_OWNER_SCOPES
            or effect_kind not in _DOCTOR_EFFECT_KINDS
        ):
            continue
        key = (str(owner_scope), str(effect_kind))
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "none"
    return ",".join(
        f"{owner_scope}/{effect_kind}={count}"
        for (owner_scope, effect_kind), count in sorted(counts.items())
    )


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
