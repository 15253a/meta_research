"""Bounded handshake/tool-discovery checks using the managed native app-server.

No model turn, native session, or tools/call request is created by this module.
Protocol responses and runner stderr are never persisted or returned verbatim.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meta_research.provider_supervisor import (
    ProviderProcessPlatform,
    WindowsProviderJob,
)
from meta_research.system_mcp import (
    NATIVE_MCP_PROFILE_NAME,
    SystemMcpConflictError,
    SystemMcpRegistry,
    compile_snapshot,
    ensure_native_mcp_profile,
)


_ACTIVE_LOCK = threading.Lock()
_ACTIVE: set[tuple[str, str]] = set()
_MAX_ACTIVE = 2
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024


class _ProbeFailed(Exception):
    pass


def _native_status(argv: list[str], environment: dict[str, str], timeout: float, *, cwd: str) -> dict[str, Any]:
    platform = ProviderProcessPlatform()
    job = WindowsProviderJob() if os.name == "nt" else None
    process = None
    reader = None
    messages: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
    output_overflow = threading.Event()
    deadline = time.monotonic() + timeout

    def drain(stream):
        size = 0
        try:
            while True:
                line = stream.readline(_MAX_OUTPUT_BYTES + 1)
                size += len(line)
                if size > _MAX_OUTPUT_BYTES:
                    output_overflow.set()
                    break
                messages.put(line or None, timeout=0.2)
                if not line:
                    break
        except (OSError, ValueError, queue.Full):
            output_overflow.set()

    try:
        options = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "env": environment, "cwd": cwd, **platform.provider_spawn_options()}
        process = job.spawn(argv, **options) if job else subprocess.Popen(argv, **options)
        reader = threading.Thread(target=drain, args=(process.stdout,), daemon=True)
        reader.start()

        def send(value):
            process.stdin.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
            process.stdin.flush()

        def rpc(identity, method, params):
            send({"id": identity, "method": method, "params": params})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if output_overflow.is_set():
                    raise _ProbeFailed("native_probe_output_limit")
                try:
                    line = messages.get(timeout=min(remaining, 0.2))
                except queue.Empty:
                    continue
                if line is None:
                    raise _ProbeFailed("native_probe_exited")
                try:
                    message = json.loads(line)
                except (UnicodeError, ValueError):
                    raise _ProbeFailed("native_probe_invalid_response") from None
                if not isinstance(message, dict) or message.get("id") != identity:
                    continue
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise _ProbeFailed("native_probe_unsupported")
                return message["result"]

        rpc(1, "initialize", {"clientInfo": {"name": "meta-research-mcp-check", "version": "1"}, "capabilities": {"experimentalApi": True}})
        send({"method": "initialized", "params": {}})
        return rpc(2, "mcpServerStatus/list", {"detail": "toolsAndAuthOnly"})
    finally:
        if process is not None:
            try:
                if job:
                    job.terminate()
                else:
                    platform.terminate_process_group(process.pid)
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
        if reader is not None:
            reader.join(timeout=0.5)
        if job is not None:
            job.close()


def check_system_mcp(
    registry: SystemMcpRegistry,
    server_id: str,
    *,
    expected_revision: int,
    executable: str,
    environment: dict[str, str],
) -> dict[str, Any]:
    """Return and persist evidence for this exact configuration revision only."""
    snapshot = registry.check_snapshot(server_id, expected_revision=expected_revision)
    server = snapshot["servers"][0]
    identity = (str(registry.path.resolve()), server_id)
    with _ACTIVE_LOCK:
        if identity in _ACTIVE or len(_ACTIVE) >= _MAX_ACTIVE:
            raise SystemMcpConflictError("system_mcp_check_busy")
        _ACTIVE.add(identity)
    try:
        arguments, sensitive = compile_snapshot(snapshot)
        # Match the host provider environment, without creating an internal MCP
        # session credential just to check an external service.
        child_environment = {key: value for key, value in dict(environment, **sensitive).items() if not key.upper().startswith("META_RESEARCH_")}
        result = {
            "server_id": server_id, "revision": server["revision"],
            "registry_revision": snapshot["registry_revision"], "status": "unknown",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "source": "native_tool_discovery",
        }
        try:
            # A check has no model/account session. Isolating native state avoids
            # contention with live roots while commands, cwd, headers and the
            # external service environment remain identical to real execution.
            with tempfile.TemporaryDirectory(prefix="meta-research-mcp-check-") as native_home:
                child_environment.update(CODEX_HOME=native_home, CODEX_SQLITE_HOME=native_home)
                ensure_native_mcp_profile(native_home)
                # app-server has no named-profile option. Its private user layer
                # performs the same reset, including inherited system MCP maps.
                shutil.copyfile(Path(native_home) / (NATIVE_MCP_PROFILE_NAME + ".config.toml"), Path(native_home) / "config.toml")
                # Per-layer strict validation rejects the deliberate map reset;
                # the final merged configuration is still native type-validated.
                status = _native_status([executable, "app-server", *arguments[2:]], child_environment, float(server["startup_timeout_sec"]) + 5, cwd=native_home)
            items = status.get("data", [])
            item = next((item for item in items if isinstance(item, dict) and item.get("name") == "external_" + server_id), None) if isinstance(items, list) else None
            if item is not None and item.get("toolsError") is not None:
                result.update(status="failed", error_code="system_mcp_connection_failed")
            elif item is not None and isinstance(item.get("serverInfo"), dict) and isinstance(item.get("tools"), dict):
                result.update(status="connected", tool_count=len(item["tools"]))
            else:
                result["error_code"] = "system_mcp_connection_evidence_unavailable"
        except TimeoutError:
            result.update(status="failed", error_code="system_mcp_check_timeout")
        except _ProbeFailed as error:
            result.update(status="unknown", error_code=str(error))
        except (OSError, subprocess.SubprocessError):
            result.update(status="failed", error_code="system_mcp_native_runner_failed")
        registry.record_check(result)
        return result
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(identity)
