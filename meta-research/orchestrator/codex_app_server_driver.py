"""One-turn Codex app-server transport for resident runtime-MCP stages.

This module is intentionally not a general app-server client.  It speaks only
the small 0.144.5 protocol slice needed to start/resume one resident owner
turn, relay the server's exact JSONL bytes, and obtain durable native-child
lineage through ``thread/read``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Optional


_SPEC_VERSION = 1
_MAX_SPEC_BYTES = 8 * 1024 * 1024
_MAX_LINE_BYTES = 4 * 1024 * 1024
_MAX_STREAM_BYTES = 32 * 1024 * 1024
_MAX_DIAGNOSTIC_BYTES = 64 * 1024
_SHUTDOWN_TIMEOUT_S = 5.0
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_READ_PREFIX = "native-review-read:"
_SPEC_KEYS = frozenset({
    "version",
    "expected_codex_home",
    "expected_codex_sqlite_home",
    "model",
    "effort",
    "cwd",
    "runtime_workspace_roots",
    "approval_policy",
    "sandbox_mode",
    "network_access",
    "config",
    "required_mcp_servers",
    "prompt",
    "thread_id",
})
_OPT_OUT_NOTIFICATIONS = [
    "command/exec/outputDelta",
    "item/agentMessage/delta",
    "item/plan/delta",
    "item/fileChange/outputDelta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
    "turn/diff/updated",
]


class AppServerDriverError(RuntimeError):
    """The installed app-server did not satisfy the narrow owner contract."""


def _strict_object(raw: bytes, *, label: str) -> dict[str, Any]:
    def unique(pairs):  # noqa: ANN001 - json object hook protocol
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise AppServerDriverError(
                    f"{label} contains duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        decoded = raw.decode("utf-8", "strict")
        value = json.loads(
            decoded, object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                AppServerDriverError(
                    f"{label} contains invalid JSON constant: {token}")))
    except AppServerDriverError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AppServerDriverError(
            f"{label} is malformed JSON: {type(error).__name__}") from error
    if not isinstance(value, dict):
        raise AppServerDriverError(f"{label} must be a JSON object")
    return value


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise AppServerDriverError(f"invalid {label}")
    return value


def _absolute_path(value: Any, label: str, *, vepfs: bool = False) -> str:
    if (not isinstance(value, str) or not value or "\x00" in value
            or not os.path.isabs(value)):
        raise AppServerDriverError(f"{label} must be an absolute path")
    normalized = os.path.abspath(value)
    if normalized != value:
        raise AppServerDriverError(f"{label} must be normalized")
    if vepfs and not normalized.startswith("/vepfs"):
        raise AppServerDriverError(f"{label} is not VEPFS-bound")
    return normalized


def _executable(path: str, label: str) -> str:
    candidate = _absolute_path(path, label)
    try:
        info = os.stat(candidate)
    except OSError as error:
        raise AppServerDriverError(f"{label} is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or not os.access(candidate, os.X_OK):
        raise AppServerDriverError(f"{label} is not a regular executable")
    return candidate


def resolve_direct_codex_bin(
        environ: Optional[Mapping[str, str]] = None) -> str:
    """Resolve only a direct CLI entry point that can honor bound storage."""
    env = os.environ if environ is None else environ
    explicit = env.get("METARESEARCH_CODEX_BIN")
    if explicit:
        return _executable(explicit, "METARESEARCH_CODEX_BIN")
    managed_root = env.get("CODEX_MANAGED_PACKAGE_ROOT")
    if managed_root:
        root = _absolute_path(
            managed_root, "CODEX_MANAGED_PACKAGE_ROOT")
        return _executable(
            str(Path(root) / "bin" / "codex.js"),
            "CODEX_MANAGED_PACKAGE_ROOT/bin/codex.js")
    return _executable("/usr/local/bin/codex", "direct Codex CLI")


def _load_spec(path: Path, env: Mapping[str, str]) -> dict[str, Any]:
    try:
        before = path.lstat()
    except OSError as error:
        raise AppServerDriverError("driver spec is unavailable") from error
    if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1 or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 0 < before.st_size <= _MAX_SPEC_BYTES):
        raise AppServerDriverError(
            "driver spec must be an owned 0600 bounded regular file")
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino, opened.st_size)
                != (before.st_dev, before.st_ino, before.st_size)):
            raise AppServerDriverError("driver spec identity changed")
        raw = b""
        while len(raw) < opened.st_size:
            chunk = os.read(fd, opened.st_size - len(raw))
            if not chunk:
                raise AppServerDriverError("driver spec was truncated")
            raw += chunk
    finally:
        os.close(fd)
    spec = _strict_object(raw, label="driver spec")
    if set(spec) != _SPEC_KEYS or spec.get("version") != _SPEC_VERSION:
        raise AppServerDriverError("unsupported driver spec shape/version")

    home = _absolute_path(
        spec.get("expected_codex_home"),
        "expected_codex_home", vepfs=True)
    sqlite_home = _absolute_path(
        spec.get("expected_codex_sqlite_home"),
        "expected_codex_sqlite_home", vepfs=True)
    if env.get("CODEX_HOME") != home:
        raise AppServerDriverError("CODEX_HOME does not match driver binding")
    if env.get("CODEX_SQLITE_HOME") != sqlite_home:
        raise AppServerDriverError(
            "CODEX_SQLITE_HOME does not match driver binding")
    cwd = _absolute_path(spec.get("cwd"), "cwd", vepfs=True)
    roots = spec.get("runtime_workspace_roots")
    if (not isinstance(roots, list) or not roots
            or len(roots) > 16):
        raise AppServerDriverError(
            "runtime_workspace_roots must be a bounded non-empty list")
    normalized_roots = [
        _absolute_path(value, "runtime workspace root", vepfs=True)
        for value in roots]
    if len(set(normalized_roots)) != len(normalized_roots):
        raise AppServerDriverError("runtime workspace roots contain duplicates")
    for key in ("model", "effort", "prompt"):
        value = spec.get(key)
        if (not isinstance(value, str) or not value
                or "\x00" in value):
            raise AppServerDriverError(f"invalid {key}")
    if len(spec["prompt"].encode("utf-8")) > _MAX_SPEC_BYTES:
        raise AppServerDriverError("prompt exceeds driver bound")
    if spec.get("approval_policy") != "never":
        raise AppServerDriverError("resident app-server approval must be never")
    if spec.get("sandbox_mode") not in {
            "read-only", "workspace-write", "danger-full-access"}:
        raise AppServerDriverError("invalid sandbox_mode")
    if not isinstance(spec.get("network_access"), bool):
        raise AppServerDriverError("network_access must be boolean")
    if (spec["sandbox_mode"] == "danger-full-access"
            and spec["network_access"] is not True):
        raise AppServerDriverError(
            "danger-full-access app-server must preserve network access")
    if not isinstance(spec.get("config"), dict):
        raise AppServerDriverError("config must be an object")
    # Round-tripping here supplies a simple bounded JSON-value check without
    # accepting NaN/Infinity or custom Python objects.
    try:
        json.dumps(
            spec["config"], allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise AppServerDriverError("config contains a non-JSON value") from error
    required_mcp = spec.get("required_mcp_servers")
    if (not isinstance(required_mcp, list)
            or len(required_mcp) > 16):
        raise AppServerDriverError(
            "required_mcp_servers must be a bounded list")
    normalized_mcp = [
        _identity(value, "required MCP server name")
        for value in required_mcp]
    if len(set(normalized_mcp)) != len(normalized_mcp):
        raise AppServerDriverError("required MCP servers contain duplicates")
    thread_id = spec.get("thread_id")
    if thread_id is not None:
        _identity(thread_id, "resident thread id")
    spec["expected_codex_home"] = home
    spec["expected_codex_sqlite_home"] = sqlite_home
    spec["cwd"] = cwd
    spec["runtime_workspace_roots"] = normalized_roots
    spec["required_mcp_servers"] = normalized_mcp
    return spec


def _sandbox_policy(spec: Mapping[str, Any]) -> dict[str, Any]:
    mode = spec["sandbox_mode"]
    if mode == "danger-full-access":
        return {"type": "dangerFullAccess"}
    if mode == "read-only":
        return {
            "type": "readOnly",
            "networkAccess": spec["network_access"],
        }
    return {
        "type": "workspaceWrite",
        "writableRoots": list(spec["runtime_workspace_roots"]),
        "networkAccess": spec["network_access"],
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def _validate_thread_response(
        event: dict[str, Any], spec: Mapping[str, Any]) -> tuple[str, str]:
    if event.get("id") != 1 or not isinstance(event.get("result"), dict):
        raise AppServerDriverError("invalid thread start/resume response")
    result = event["result"]
    thread = result.get("thread")
    if (not isinstance(thread, dict)
            or thread.get("parentThreadId") is not None):
        raise AppServerDriverError("invalid parent thread identity")
    parent = _identity(thread.get("id"), "parent thread id")
    requested = spec.get("thread_id")
    if requested is not None and parent != requested:
        raise AppServerDriverError("resumed parent thread identity drift")
    if result.get("model") != spec["model"]:
        raise AppServerDriverError("effective model drift")
    if result.get("cwd") != spec["cwd"]:
        raise AppServerDriverError("effective cwd drift")
    if result.get("runtimeWorkspaceRoots") != spec["runtime_workspace_roots"]:
        raise AppServerDriverError("effective workspace roots drift")
    if result.get("approvalPolicy") != spec["approval_policy"]:
        raise AppServerDriverError("effective approval policy drift")
    sandbox = result.get("sandbox")
    if not isinstance(sandbox, dict):
        raise AppServerDriverError("effective sandbox is missing")
    expected_type = {
        "read-only": "readOnly",
        "workspace-write": "workspaceWrite",
        "danger-full-access": "dangerFullAccess",
    }[spec["sandbox_mode"]]
    if sandbox.get("type") != expected_type:
        raise AppServerDriverError("effective sandbox mode drift")
    if (expected_type != "dangerFullAccess"
            and sandbox.get("networkAccess") != spec["network_access"]):
        raise AppServerDriverError("effective sandbox network drift")
    return parent, requested or parent


def _thread_request(spec: Mapping[str, Any]) -> dict[str, Any]:
    params = {
        "model": spec["model"],
        "cwd": spec["cwd"],
        "runtimeWorkspaceRoots": list(spec["runtime_workspace_roots"]),
        "approvalPolicy": spec["approval_policy"],
        "sandbox": spec["sandbox_mode"],
        "config": spec["config"],
    }
    if spec.get("thread_id") is None:
        params.update({
            "ephemeral": False,
            "experimentalRawEvents": True,
        })
        method = "thread/start"
    else:
        params["threadId"] = spec["thread_id"]
        params["excludeTurns"] = True
        method = "thread/resume"
    return {"id": 1, "method": method, "params": params}


def _turn_request(spec: Mapping[str, Any], parent: str) -> dict[str, Any]:
    return {
        "id": 2,
        "method": "turn/start",
        "params": {
            "threadId": parent,
            "input": [{
                "type": "text",
                "text": spec["prompt"],
                "text_elements": [],
            }],
            "cwd": spec["cwd"],
            "runtimeWorkspaceRoots": list(spec["runtime_workspace_roots"]),
            "approvalPolicy": spec["approval_policy"],
            "sandboxPolicy": _sandbox_policy(spec),
            "model": spec["model"],
            "effort": spec["effort"],
        },
    }


class _OwnerExchange:
    def __init__(
            self, process: subprocess.Popen, stdout: BinaryIO,
            spec: Mapping[str, Any]):
        self.process = process
        self.stdout = stdout
        self.spec = spec
        self.stream_bytes = 0
        self.parent_thread: Optional[str] = None
        self.parent_turn: Optional[str] = None
        self.parent_terminal = False
        self.children: set[str] = set()
        self.child_terminal: set[str] = set()
        self.child_reads: set[str] = set()
        self.required_mcp = set(spec["required_mcp_servers"])
        self.ready_mcp: set[str] = set()
        self._mcp_status: dict[tuple[str, str], str] = {}
        self.stdin_closed = False

    def send(self, value: Mapping[str, Any]) -> None:
        if self.stdin_closed or self.process.stdin is None:
            raise AppServerDriverError("app-server input is closed")
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False,
            separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self.process.stdin.write(raw)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise AppServerDriverError(
                "app-server input closed unexpectedly") from error

    def close_input(self) -> None:
        if not self.stdin_closed and self.process.stdin is not None:
            self.stdin_closed = True
            self.process.stdin.close()

    def _decode_and_relay(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > _MAX_LINE_BYTES or not raw.endswith(b"\n"):
            raise AppServerDriverError(
                "app-server emitted an oversized or partial JSONL line")
        self.stream_bytes += len(raw)
        if self.stream_bytes > _MAX_STREAM_BYTES:
            raise AppServerDriverError("app-server stream exceeds bound")
        self.stdout.write(raw)
        flush = getattr(self.stdout, "flush", None)
        if callable(flush):
            flush()
        return _strict_object(raw[:-1], label="app-server event")

    def read(self) -> Optional[dict[str, Any]]:
        assert self.process.stdout is not None
        raw = self.process.stdout.readline(_MAX_LINE_BYTES + 1)
        if not raw:
            return None
        return self._decode_and_relay(raw)

    def drain_after_input_close(self, *, deadline: float) -> None:
        """Drain exact trailing JSONL without blocking beyond one deadline."""
        assert self.process.stdout is not None
        fd = self.process.stdout.fileno()
        pending = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerDriverError(
                    "app-server did not stop after input closure")
            readable, _writable, _exceptional = select.select(
                [fd], [], [], remaining)
            if not readable:
                raise AppServerDriverError(
                    "app-server did not stop after input closure")
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                if pending:
                    raise AppServerDriverError(
                        "app-server emitted a trailing partial JSONL line")
                return
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    if len(pending) > _MAX_LINE_BYTES:
                        raise AppServerDriverError(
                            "app-server emitted an oversized JSONL line")
                    break
                raw = bytes(pending[:newline + 1])
                del pending[:newline + 1]
                self.observe(self._decode_and_relay(raw))

    def bind_parent(self, parent: str) -> None:
        if self.parent_thread is not None:
            raise AppServerDriverError("parent thread is already bound")
        self.parent_thread = parent
        for (thread_id, name), status in tuple(self._mcp_status.items()):
            if thread_id == parent:
                self._apply_mcp_status(name, status)

    def _apply_mcp_status(self, name: str, status: str) -> None:
        if status == "ready":
            self.ready_mcp.add(name)
        elif status in {"failed", "cancelled"}:
            raise AppServerDriverError(
                f"required MCP server failed: {name}")

    @staticmethod
    def reject_server_request(event: Mapping[str, Any]) -> None:
        if "method" in event and "id" in event:
            raise AppServerDriverError(
                "unexpected app-server server request")

    def response(self, request_id: int) -> dict[str, Any]:
        while True:
            event = self.read()
            if event is None:
                raise AppServerDriverError(
                    f"app-server ended before response {request_id}")
            self.reject_server_request(event)
            if event.get("id") == request_id:
                if event.get("error") is not None:
                    raise AppServerDriverError(
                        f"app-server request {request_id} failed")
                return event
            # Notifications may legally interleave with request responses.
            # They are already relayed byte-for-byte; retain the same owner
            # state (notably required-MCP readiness) instead of discarding it.
            self.observe(event)

    def observe(self, event: dict[str, Any]) -> None:
        self.reject_server_request(event)
        if event.get("error") is not None and "id" in event:
            raise AppServerDriverError("app-server request failed")
        response_id = event.get("id")
        if (isinstance(response_id, str)
                and response_id.startswith(_READ_PREFIX)):
            child = response_id[len(_READ_PREFIX):]
            if child not in self.child_terminal or child in self.child_reads:
                raise AppServerDriverError("unexpected child thread/read response")
            result = event.get("result")
            if (not isinstance(result, dict)
                    or not isinstance(result.get("thread"), dict)
                    or result["thread"].get("id") != child):
                raise AppServerDriverError("invalid child thread/read response")
            self.child_reads.add(child)
            return
        method = event.get("method")
        params = event.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        if method == "mcpServer/startupStatus/updated":
            name = params.get("name")
            if name in self.required_mcp:
                thread_id = _identity(
                    params.get("threadId"), "MCP status thread id")
                status = params.get("status")
                if not isinstance(status, str):
                    raise AppServerDriverError("invalid MCP startup status")
                key = (thread_id, name)
                previous = self._mcp_status.get(key)
                if previous not in {"failed", "cancelled"}:
                    self._mcp_status[key] = status
                if thread_id == self.parent_thread:
                    self._apply_mcp_status(name, self._mcp_status[key])
            return
        item = params.get("item")
        if (method == "item/completed" and isinstance(item, dict)
                and item.get("type") == "subAgentActivity"
                and item.get("kind") == "started"):
            if (params.get("threadId") != self.parent_thread
                    or params.get("turnId") != self.parent_turn):
                raise AppServerDriverError(
                    "child activity has wrong parent identity")
            child = _identity(
                item.get("agentThreadId"), "child thread id")
            if child in self.children or child == self.parent_thread:
                raise AppServerDriverError("duplicate child activity")
            self.children.add(child)
            return
        if method != "turn/completed":
            return
        thread_id = params.get("threadId")
        turn = params.get("turn")
        if not isinstance(turn, dict):
            raise AppServerDriverError("malformed turn completion")
        if thread_id == self.parent_thread:
            if (turn.get("id") != self.parent_turn
                    or turn.get("status") != "completed"
                    or self.parent_terminal):
                raise AppServerDriverError(
                    "parent turn did not complete cleanly")
            self.parent_terminal = True
            return
        if thread_id in self.children:
            if (turn.get("status") != "completed"
                    or thread_id in self.child_terminal):
                raise AppServerDriverError(
                    "native child turn did not complete cleanly")
            self.child_terminal.add(thread_id)
            self.send({
                "id": _READ_PREFIX + thread_id,
                "method": "thread/read",
                "params": {"threadId": thread_id, "includeTurns": True},
            })

    def complete(self) -> bool:
        return (
            self.parent_terminal
            and self.children == self.child_terminal == self.child_reads
            and self.required_mcp <= self.ready_mcp)

    def incomplete_reason(self) -> str:
        unresolved = self.children - self.child_terminal
        unread = self.child_terminal - self.child_reads
        if unresolved or unread:
            return "unresolved child native-review activity"
        if not self.parent_terminal:
            return "parent turn did not reach terminal completion"
        missing_mcp = self.required_mcp - self.ready_mcp
        if missing_mcp:
            return "required MCP server did not become ready"
        return "app-server owner exchange is incomplete"


def run_driver_spec(
        spec_path: Path, *, environ: Optional[Mapping[str, str]] = None,
        stdout: Optional[BinaryIO] = None,
        stderr: Optional[BinaryIO] = None) -> None:
    """Run one trusted spec and relay every inbound server JSONL line."""
    env = dict(os.environ if environ is None else environ)
    output = sys.stdout.buffer if stdout is None else stdout
    diagnostics = sys.stderr.buffer if stderr is None else stderr
    path = Path(spec_path)
    spec = _load_spec(path, env)
    codex_bin = resolve_direct_codex_bin(env)
    command = [
        codex_bin,
        "app-server", "--listen", "stdio://",
        "--enable", "multi_agent",
        "--enable", "multi_agent_v2",
        "--enable", "enable_fanout",
    ]
    process: Optional[subprocess.Popen] = None
    exchange: Optional[_OwnerExchange] = None
    primary: Optional[BaseException] = None
    with tempfile.TemporaryFile() as server_stderr:
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=server_stderr, cwd=spec["cwd"], env=env,
                bufsize=0)
            exchange = _OwnerExchange(process, output, spec)
            exchange.send({
                "id": 0,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "meta-research-resident-driver",
                        "title": "Meta Research Resident Driver",
                        "version": "1",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                        "optOutNotificationMethods": _OPT_OUT_NOTIFICATIONS,
                    },
                },
            })
            initialized = exchange.response(0)
            result = initialized.get("result")
            if (not isinstance(result, dict)
                    or result.get("codexHome")
                    != spec["expected_codex_home"]):
                raise AppServerDriverError(
                    "initialize codexHome does not match VEPFS binding")
            if (result.get("platformFamily") != "unix"
                    or result.get("platformOs") != "linux"):
                raise AppServerDriverError(
                    "unsupported app-server platform identity")
            exchange.send({"method": "initialized"})
            exchange.send(_thread_request(spec))
            thread_event = exchange.response(1)
            parent, _expected_parent = _validate_thread_response(
                thread_event, spec)
            exchange.bind_parent(parent)
            exchange.send(_turn_request(spec, parent))
            turn_event = exchange.response(2)
            turn = turn_event.get("result", {}).get("turn")
            if (not isinstance(turn, dict)
                    or turn.get("status") != "inProgress"):
                raise AppServerDriverError("invalid parent turn response")
            exchange.parent_turn = _identity(
                turn.get("id"), "parent turn id")

            while not exchange.complete():
                event = exchange.read()
                if event is None:
                    break
                exchange.observe(event)
            if not exchange.complete():
                raise AppServerDriverError(exchange.incomplete_reason())
            exchange.close_input()
            shutdown_deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_S
            exchange.drain_after_input_close(deadline=shutdown_deadline)
            if not exchange.complete():
                raise AppServerDriverError(exchange.incomplete_reason())
            remaining = shutdown_deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerDriverError(
                    "app-server did not stop after input closure")
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                raise AppServerDriverError(
                    "app-server did not stop after input closure") from error
            if returncode != 0:
                raise AppServerDriverError(
                    f"app-server exited non-cleanly: {returncode}")
        except BaseException as error:
            primary = error
            raise
        finally:
            if exchange is not None:
                try:
                    exchange.close_input()
                except BaseException as error:
                    if primary is None:
                        primary = error
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            server_stderr.seek(0, os.SEEK_END)
            size = server_stderr.tell()
            server_stderr.seek(0)
            diagnostic = server_stderr.read(_MAX_DIAGNOSTIC_BYTES)
            if diagnostic:
                diagnostics.write(diagnostic)
                flush = getattr(diagnostics, "flush", None)
                if callable(flush):
                    flush()
            if size > _MAX_DIAGNOSTIC_BYTES and primary is None:
                raise AppServerDriverError(
                    "app-server diagnostic stream exceeds bound")


def extract_parent_final(raw: bytes) -> tuple[str, bytes]:
    """Extract only the bound parent turn's authoritative final answer."""
    if not isinstance(raw, bytes) or len(raw) > _MAX_STREAM_BYTES:
        raise AppServerDriverError("captured app-server stream is invalid")
    if raw and not raw.endswith(b"\n"):
        raise AppServerDriverError("captured app-server stream is partial")
    parent: Optional[str] = None
    turn_id: Optional[str] = None
    final: Optional[bytes] = None
    terminal = False
    for raw_line in raw.splitlines():
        if len(raw_line) > _MAX_LINE_BYTES:
            raise AppServerDriverError("captured app-server line exceeds bound")
        event = _strict_object(raw_line, label="captured app-server event")
        if event.get("id") == 1 and isinstance(event.get("result"), dict):
            thread = event["result"].get("thread")
            if not isinstance(thread, dict) or parent is not None:
                raise AppServerDriverError("invalid captured parent thread")
            parent = _identity(thread.get("id"), "captured parent thread")
            continue
        if event.get("id") == 2 and isinstance(event.get("result"), dict):
            turn = event["result"].get("turn")
            if not isinstance(turn, dict) or turn_id is not None:
                raise AppServerDriverError("invalid captured parent turn")
            turn_id = _identity(turn.get("id"), "captured parent turn")
            continue
        params = event.get("params")
        if (event.get("method") == "item/completed"
                and isinstance(params, dict)
                and params.get("threadId") == parent
                and params.get("turnId") == turn_id):
            item = params.get("item")
            if (isinstance(item, dict)
                    and item.get("type") == "agentMessage"
                    and item.get("phase") == "final_answer"):
                text = item.get("text")
                if not isinstance(text, str) or final is not None:
                    raise AppServerDriverError(
                        "invalid captured parent final")
                final = text.encode("utf-8")
        if (event.get("method") == "turn/completed"
                and isinstance(params, dict)
                and params.get("threadId") == parent):
            turn = params.get("turn")
            if (not isinstance(turn, dict)
                    or turn.get("id") != turn_id
                    or turn.get("status") != "completed"
                    or terminal):
                raise AppServerDriverError(
                    "invalid captured parent terminal")
            terminal = True
    if parent is None or turn_id is None or final is None or not terminal:
        raise AppServerDriverError(
            "captured parent final/terminal is incomplete")
    return parent, final


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one bounded resident Codex app-server turn")
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    try:
        run_driver_spec(Path(args.spec))
    except AppServerDriverError as error:
        message = f"app-server driver failed: {error}\n".encode("utf-8")
        sys.stderr.buffer.write(message[:_MAX_DIAGNOSTIC_BYTES])
        sys.stderr.buffer.flush()
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
