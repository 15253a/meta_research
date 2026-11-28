"""Codex 0.159 app-server transport inside the existing durable supervisor.

Only native thread/start, thread/resume, turn/start and turn/steer create
provider inputs. A prepared or uncertain steer is never replayed as a new turn.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from meta_research.owners.common import canonical_hash
from meta_research.human_guidance import live_guidance_prompt

from meta_research.provider_supervisor import (
    read_transport_key_for_operation, read_transport_envelope, write_transport_envelope, read_supervisor_request,
)


MAX_STDOUT_BYTES = 64 * 1024 * 1024
MAX_STDOUT_LINE_BYTES = 64 * 1024 * 1024
DEFAULT_TURN_IDLE_TIMEOUT_SECONDS = 900.0
DEFAULT_TURN_ACTIVE_TOOL_IDLE_TIMEOUT_SECONDS = 3600.0
DEFAULT_TURN_INTERRUPT_GRACE_SECONDS = 2.0
_READ_CHUNK_BYTES = 65536
_ACTIVE_TOOL_ITEM_TYPES = frozenset({"commandExecution", "collabAgentToolCall", "mcpToolCall"})
_HTTP_CAPACITY_STATUS_RE = re.compile(
    r"(?:\b(?:http|status|code|error|err)\s*[:=_\-/ ]*\s*(?:429|529)\b"
    r"|\b(?:429|529)\s+(?:too many requests|overloaded|rate limit|capacity)\b)"
)
_HTTP_TRANSIENT_STATUS_RE = re.compile(
    r"(?:\b(?:http|status|code|error|err)\s*[:=_\-/ ]*\s*(?:502|503|504)\b"
    r"|\b(?:502|503|504)\s+(?:bad gateway|service unavailable|gateway timeout)\b)"
)
_KNOWN_UPSTREAM_CLASSIFICATIONS = frozenset(
    {"upstream_capacity", "upstream_transient", "usage_limit", "native_turn_idle_timeout"}
)


def _env_positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def classify_native_error(error_payload, *, will_retry: bool | None = None) -> str | None:
    """Classify structured or text Codex app-server errors for upstream backoff."""
    if isinstance(error_payload, dict) and "error" in error_payload and isinstance(error_payload.get("error"), (dict, str)):
        inner = error_payload["error"]
        if will_retry is None and isinstance(error_payload.get("willRetry"), bool):
            will_retry = error_payload["willRetry"]
        error_payload = inner

    texts: list[str] = []
    if isinstance(error_payload, dict):
        info = error_payload.get("codexErrorInfo")
        if isinstance(info, str):
            norm = info.strip().lower().replace("-", "_")
            if norm in {"usagelimitexceeded", "usage_limit_exceeded"}:
                return "usage_limit"
            if norm in {
                "modelcap", "model_cap", "serveroverloaded", "server_overloaded",
                "ratelimitexceeded", "rate_limit_exceeded", "capacity", "overloaded",
            }:
                return "upstream_capacity"
            if norm in {
                "responsestreamdisconnected", "response_stream_disconnected",
                "responsetoomanyfailedattempts", "response_too_many_failed_attempts",
                "internalservererror", "internal_server_error", "upstreamtransient",
            }:
                return "upstream_transient"
        elif isinstance(info, dict):
            candidates = [info]
            if isinstance(info.get("httpConnectionFailed"), dict):
                candidates.append(info["httpConnectionFailed"])
            for candidate in candidates:
                for key in ("httpStatusCode", "statusCode", "status"):
                    status = candidate.get(key)
                    if isinstance(status, int) and not isinstance(status, bool):
                        if status in {429, 529}:
                            return "upstream_capacity"
                        if status in {500, 502, 503, 504}:
                            return "upstream_transient"
        for field in ("message", "additionalDetails", "code", "reason"):
            val = error_payload.get(field)
            if isinstance(val, str) and val.strip():
                texts.append(val)
    elif isinstance(error_payload, str) and error_payload.strip():
        texts.append(error_payload)

    combined = " ".join(texts).replace("\u2019", "'").lower()
    if combined:
        if "you've hit your usage limit" in combined or "usage limit exceeded" in combined:
            return "usage_limit"
        if any(
            token in combined
            for token in (
                "at capacity",
                "overloaded",
                "too many requests",
                "rate limit",
                "rate_limit",
                "capacity exceeded",
                "status 429",
                "status 529",
            )
        ) or _HTTP_CAPACITY_STATUS_RE.search(combined):
            return "upstream_capacity"
        if any(
            token in combined
            for token in (
                "temporarily unavailable",
                "service unavailable",
                "bad gateway",
                "gateway timeout",
                "connection reset",
                "stream disconnected",
                "reconnecting",
                "upstream timeout",
                "upstream_timeout",
                "upstream transient",
                "upstream_transient",
                "upstream error",
                "upstream_error",
                "upstream connect",
                "upstream reset",
                "native_turn_idle_timeout",
                "status 502",
                "status 503",
                "status 504",
            )
        ) or _HTTP_TRANSIENT_STATUS_RE.search(combined):
            return "upstream_transient"
    if will_retry is True:
        return "upstream_transient"
    return None


def extract_native_error_classification(events_or_stdout, *, native_error=None) -> str | None:
    """Extract structured upstream classification from bridge stdout JSONL or native_error."""
    if native_error is not None and native_error != {}:
        if isinstance(native_error, dict) and native_error.get("code") == "native_turn_idle_timeout":
            return "native_turn_idle_timeout"
        return classify_native_error(native_error)

    if isinstance(events_or_stdout, str):
        events: list[dict[str, object]] = []
        for line in events_or_stdout.splitlines():
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
    elif isinstance(events_or_stdout, (list, tuple)):
        events = [e for e in events_or_stdout if isinstance(e, dict)]
    else:
        events = []

    last_turn_failed = None
    for event in reversed(events):
        if event.get("type") == "turn.failed":
            last_turn_failed = event
            break
    if last_turn_failed is not None:
        raw_cls = last_turn_failed.get("classification")
        err = last_turn_failed.get("error")
        if isinstance(err, dict) and err.get("code") == "native_turn_idle_timeout":
            return "native_turn_idle_timeout"
        if isinstance(raw_cls, str) and raw_cls in _KNOWN_UPSTREAM_CLASSIFICATIONS:
            return raw_cls
        if err is not None and err != {}:
            # Terminal error is present and did not classify as upstream; do not
            # inherit an earlier recovered transient blip.
            return classify_native_error(err)

    for event in reversed(events):
        if event.get("type") == "error":
            err = event.get("error")
            if isinstance(err, dict) and err.get("code") == "native_turn_idle_timeout":
                return "native_turn_idle_timeout"
            raw_cls = event.get("classification")
            if isinstance(raw_cls, str) and raw_cls in _KNOWN_UPSTREAM_CLASSIFICATIONS:
                return raw_cls
            return classify_native_error(err if err is not None else event.get("message"))
    return None


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def _settings(argv):
    if (not isinstance(argv, list) or not all(isinstance(argument, str) and argument for argument in argv)
            or len(argv) < 3 or argv[1] != "exec"):
        raise ValueError("native_codex_exec_arguments_invalid")
    command, options = [argv[0], "app-server"], {}
    index = 2
    while index < len(argv):
        flag = argv[index]
        if flag in {"--config", "-c", "--enable", "--disable", "--model", "-m", "--cd", "-C",
                    "--sandbox", "-s", "--output-schema", "--output-last-message", "resume"} and index + 1 >= len(argv):
            raise ValueError("native_codex_exec_arguments_invalid")
        if flag in {"--config", "-c", "--enable", "--disable"}:
            command.extend(argv[index:index + 2]); index += 2
        elif flag == "--strict-config":
            command.append(flag); index += 1
        elif flag in {"--model", "-m", "--cd", "-C", "--sandbox", "-s",
                      "--output-schema", "--output-last-message"}:
            options[flag] = argv[index + 1]; index += 2
        elif flag == "resume":
            options["resume"] = argv[index + 1]; index += 2
        elif flag in {"--skip-git-repo-check", "--json", "-"}:
            index += 1
        else:
            raise ValueError("native_codex_exec_option_unsupported:" + flag)
    return command, options


def _exec_item(item):
    """Translate public native item spelling without manufacturing evidence."""
    names = {"agentMessage": "agent_message", "commandExecution": "command_execution",
             "fileChange": "file_change", "mcpToolCall": "mcp_tool_call",
             "collabAgentToolCall": "collab_tool_call", "webSearch": "web_search",
             "imageView": "image_view", "contextCompaction": "context_compaction",
             "reasoning": "reasoning"}
    kind = item.get("type")
    if kind not in names:
        return None
    fields = {"aggregatedOutput": "aggregated_output", "exitCode": "exit_code",
              "senderThreadId": "sender_thread_id", "receiverThreadIds": "receiver_thread_ids",
              "agentsStates": "agents_states", "processId": "process_id"}
    translated = {fields.get(key, key): value for key, value in item.items() if key != "type"} | {"type": names[kind]}
    if kind == "collabAgentToolCall":
        translated["tool"] = {"spawnAgent": "spawn_agent", "sendInput": "send_input",
            "resumeAgent": "resume_agent", "closeAgent": "close_agent"}.get(item.get("tool"), item.get("tool"))
    return translated


class NativeRpcError(ValueError):
    def __init__(self, method, error):
        super().__init__("native_request_rejected:" + method)
        self.method, self.error = method, error


class NativeTerminalTurnError(RuntimeError):
    """The exact original native turn has an authenticated failure outcome."""
    def __init__(self, *, status, native_session_ref, stdout, checkpoint, classification=None):
        super().__init__("provider_native_turn_" + status)
        self.status = status
        self.native_session_ref = native_session_ref
        self.stdout = stdout
        self.checkpoint = checkpoint
        self.classification = classification


class NativeCodexBridge:
    def __init__(
        self,
        argv,
        directory,
        prompt,
        *,
        turn_idle_timeout_seconds: float | None = None,
        turn_active_tool_idle_timeout_seconds: float | None = None,
        turn_interrupt_grace_seconds: float | None = None,
        max_stdout_bytes: int | None = None,
        max_stdout_line_bytes: int | None = None,
    ):
        self.directory = Path(directory)
        self.key_path, self.key = read_transport_key_for_operation(self.directory)
        self.command, self.options = _settings(argv)
        self.provider_argv_hash = canonical_hash(argv)
        self.prompt = prompt
        self.thread_id = self.turn_id = None
        self.terminal = None
        self.last_message = None
        self.last_error = None
        self.last_error_classification = None
        self._next_id = 0
        self._records = queue.Queue(maxsize=64)
        self._reader_stop = threading.Event()
        self._seen = set()
        self._next_reconcile = {}
        self._active_item_ids: set[str] = set()
        self._anonymous_active_items = 0
        self.process = None
        self.usage = None
        self.turn_idle_timeout_seconds = (
            _env_positive_float(
                "META_RESEARCH_NATIVE_TURN_IDLE_TIMEOUT_SECONDS",
                DEFAULT_TURN_IDLE_TIMEOUT_SECONDS,
            )
            if turn_idle_timeout_seconds is None
            else turn_idle_timeout_seconds
        )
        self.turn_active_tool_idle_timeout_seconds = (
            _env_positive_float(
                "META_RESEARCH_NATIVE_ACTIVE_TOOL_IDLE_TIMEOUT_SECONDS",
                DEFAULT_TURN_ACTIVE_TOOL_IDLE_TIMEOUT_SECONDS,
            )
            if turn_active_tool_idle_timeout_seconds is None
            else turn_active_tool_idle_timeout_seconds
        )
        self.turn_interrupt_grace_seconds = (
            _env_positive_float(
                "META_RESEARCH_NATIVE_TURN_INTERRUPT_GRACE_SECONDS",
                DEFAULT_TURN_INTERRUPT_GRACE_SECONDS,
            )
            if turn_interrupt_grace_seconds is None
            else turn_interrupt_grace_seconds
        )
        self.max_stdout_bytes = (
            _env_positive_int("META_RESEARCH_NATIVE_MAX_STDOUT_BYTES", MAX_STDOUT_BYTES)
            if max_stdout_bytes is None
            else max_stdout_bytes
        )
        default_line_limit = min(MAX_STDOUT_LINE_BYTES, self.max_stdout_bytes)
        self.max_stdout_line_bytes = (
            min(
                _env_positive_int("META_RESEARCH_NATIVE_MAX_STDOUT_LINE_BYTES", default_line_limit),
                self.max_stdout_bytes,
            )
            if max_stdout_line_bytes is None
            else min(max_stdout_line_bytes, self.max_stdout_bytes)
        )
        self._last_activity = time.monotonic()

    def _send(self, value):
        self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def _put_record(self, value):
        while not self._reader_stop.is_set():
            try:
                self._records.put(value, timeout=0.1)
                return
            except queue.Full:
                continue

    def _reader(self):
        try:
            total = 0
            line_buffer = bytearray()
            while not self._reader_stop.is_set():
                remaining_total = self.max_stdout_bytes - total
                remaining_line = self.max_stdout_line_bytes - len(line_buffer)
                step = min(_READ_CHUNK_BYTES, remaining_total + 1, remaining_line + 1)
                if step <= 0:
                    raise ValueError("native_provider_stream_limit")
                chunk = self.process.stdout.readline(step)
                if not chunk:
                    if line_buffer:
                        self._put_record(json.loads(bytes(line_buffer)))
                    break
                total += len(chunk)
                line_buffer.extend(chunk)
                if total > self.max_stdout_bytes or len(line_buffer) > self.max_stdout_line_bytes:
                    raise ValueError("native_provider_stream_limit")
                if chunk.endswith(b"\n"):
                    self._put_record(json.loads(bytes(line_buffer)))
                    line_buffer.clear()
        except Exception as error:
            self._put_record(error)
        finally:
            self._put_record(EOFError("native_provider_stream_closed"))

    def _next(self, timeout=0.1):
        value = self._records.get(timeout=timeout)
        if isinstance(value, BaseException):
            raise value
        if not isinstance(value, dict):
            raise ValueError("native_provider_record_invalid")
        return value

    def request(self, method, params):
        self._next_id += 1
        request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                record = self._next()
            except queue.Empty:
                continue
            if record.get("id") == request_id and "method" not in record:
                if "error" in record:
                    raise NativeRpcError(method, record["error"])
                return record.get("result")
            self.notification(record)
        raise TimeoutError("native_request_outcome_unknown")

    def notification(self, record):
        method, params = record.get("method"), record.get("params", {})
        if "id" in record and method:
            # Approval is never delegated to a fabricated human answer. The
            # configured never policy normally prevents these requests.
            self._send({"id": record["id"], "error": {"code": -32601,
                "message": "Interactive approval is unavailable for this research operation"}})
            return
        if not isinstance(params, dict):
            return
        if self.thread_id is not None and params.get("threadId", self.thread_id) != self.thread_id:
            return
        if method != "error":
            self._last_activity = time.monotonic()
            if method != "turn/completed":
                # Real forward progress supersedes any earlier recovered transient error.
                self.last_error = None
                self.last_error_classification = None
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in _ACTIVE_TOOL_ITEM_TYPES:
                    item_id = item.get("id")
                    if method == "item/started":
                        if isinstance(item_id, str) and item_id:
                            self._active_item_ids.add(item_id)
                        else:
                            self._anonymous_active_items += 1
                    else:
                        if isinstance(item_id, str) and item_id:
                            self._active_item_ids.discard(item_id)
                        elif self._anonymous_active_items > 0:
                            self._anonymous_active_items -= 1
                if (translated := _exec_item(item)) is not None:
                    _emit({"type": "item.started" if method.endswith("started") else "item.completed",
                           "thread_id": params.get("threadId"), "turn_id": params.get("turnId"),
                           "item": translated})
                    if method == "item/completed" and item_type == "agentMessage":
                        self.last_message = item.get("text")
        elif method == "turn/started":
            turn = params.get("turn", {})
            self.turn_id = turn.get("id")
            _emit({"type": "turn.started", "turn_id": self.turn_id})
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if self.turn_id is not None and turn.get("id") != self.turn_id:
                raise ValueError("native_turn_identity_mismatch")
            self.terminal = turn
        elif method == "item/commandExecution/outputDelta":
            _emit({"type": "item.output", "item_id": params.get("itemId"),
                   "delta": params.get("delta")})
        elif method == "thread/tokenUsage/updated":
            self.usage = params.get("tokenUsage")
        elif method == "error":
            error_info = params.get("error")
            will_retry = params.get("willRetry")
            if isinstance(error_info, dict) and isinstance(error_info.get("message"), str):
                message = error_info["message"]
            elif isinstance(error_info, str):
                message = error_info
            elif isinstance(params.get("message"), str):
                message = params["message"]
            else:
                message = None
            classification = classify_native_error(
                error_info if error_info is not None else params,
                will_retry=will_retry if isinstance(will_retry, bool) else None,
            )
            error_obj = (
                error_info
                if isinstance(error_info, dict)
                else ({"message": message} if message is not None else {})
            )
            event = {
                "type": "error",
                "thread_id": params.get("threadId", self.thread_id),
                "turn_id": params.get("turnId", self.turn_id),
                "message": message,
                "error": error_obj,
                "will_retry": bool(will_retry) if isinstance(will_retry, bool) else False,
            }
            if classification is not None:
                event["classification"] = classification
            self.last_error = event
            self.last_error_classification = classification
            _emit(event)

    def _interrupt_turn_best_effort(self) -> None:
        if not self.thread_id or not self.turn_id or self.process is None:
            return
        self._next_id += 1
        interrupt_id = self._next_id
        try:
            self._send({
                "id": interrupt_id,
                "method": "turn/interrupt",
                "params": {"threadId": self.thread_id, "turnId": self.turn_id},
            })
        except OSError:
            return
        grace = self.turn_interrupt_grace_seconds
        if grace is None or grace <= 0:
            return
        deadline = time.monotonic() + grace
        while self.terminal is None and time.monotonic() < deadline:
            remaining = max(0.01, min(0.05, deadline - time.monotonic()))
            try:
                record = self._next(timeout=remaining)
            except queue.Empty:
                continue
            except (EOFError, OSError, ValueError):
                break
            if record.get("id") == interrupt_id and "method" not in record:
                continue
            try:
                self.notification(record)
            except (OSError, ValueError):
                break

    def _save_state(self, path, value):
        write_transport_envelope(path, value, self.key)

    def _observed_input(self, command_id, text):
        cursor, visited = None, set()
        while True:
            result = self.request("thread/items/list", {"threadId": self.thread_id,
                "turnId": self.turn_id, "limit": 100, "sortDirection": "desc", "cursor": cursor})
            if not isinstance(result, dict):
                raise ValueError("native_input_reconciliation_invalid")
            for entry in result.get("data", []):
                if entry.get("turnId") != self.turn_id:
                    raise ValueError("native_turn_identity_mismatch")
                item = entry.get("item", {})
                if (item.get("type") == "userMessage" and item.get("clientId") == command_id
                    and any(part.get("type") == "text" and part.get("text") == text
                            for part in item.get("content", []))):
                    return True
            cursor = result.get("nextCursor")
            if cursor is None:
                return False
            if not isinstance(cursor, str) or cursor in visited:
                raise ValueError("native_input_cursor_invalid")
            visited.add(cursor)

    def poll_guidance(self):
        if not self.thread_id or not self.turn_id or self.terminal is not None:
            return
        closed = self.directory / "live-guidance-closed.json"
        if closed.exists():
            read_transport_envelope(closed, self.key)
            return
        commands = [(path, read_transport_envelope(path, self.key))
                    for path in (self.directory / "live-guidance").glob("*.json")]
        for path, command in sorted(commands,
                key=lambda item: (item[1].get("message", {}).get("prepared_at", 0), item[0].name)):
            if self.terminal is not None:
                return
            if path.stem in self._seen:
                continue
            if closed.exists():
                read_transport_envelope(closed, self.key)
                return
            command_id = command.get("id")
            if command_id != path.stem or not isinstance(command.get("message"), dict):
                raise ValueError("native_guidance_command_invalid")
            result_path = path.with_suffix(".receipt")
            sending_path = path.with_suffix(".sending")
            if result_path.exists():
                receipt = read_transport_envelope(result_path, self.key)
                if receipt.get("id") != command_id:
                    raise ValueError("native_guidance_receipt_invalid")
                self._seen.add(command_id)
                continue
            if sending_path.exists():
                if time.monotonic() < self._next_reconcile.get(command_id, 0):
                    continue
                self._next_reconcile[command_id] = time.monotonic() + 5
                sent = read_transport_envelope(sending_path, self.key)
                if (set(sent) != {"id", "thread_id", "turn_id", "text"}
                        or sent["id"] != command_id or sent["thread_id"] != self.thread_id
                        or sent["turn_id"] != self.turn_id
                        or not isinstance(sent["text"], str) or not sent["text"]):
                    raise ValueError("native_guidance_send_identity_invalid")
                observed = self._observed_input(command_id, sent["text"])
                if observed:
                    self._save_state(result_path, {**sent, "status": "accepted", "reconciled": True})
                    self._seen.add(command_id)
                # Absence from a live or partial history is no proof of failed
                # delivery. Keep the original send unresolved; never retransmit.
                continue
            text = live_guidance_prompt(command["message"])
            sent = {"id": command_id, "thread_id": self.thread_id,
                    "turn_id": self.turn_id, "text": text}
            self._save_state(sending_path, sent)
            try:
                result = self.request("turn/steer", {"threadId": self.thread_id,
                    "expectedTurnId": self.turn_id, "clientUserMessageId": command_id,
                    "input": [{"type": "text", "text": text}]})
                if not isinstance(result, dict) or result.get("turnId") != self.turn_id:
                    raise ValueError("native_guidance_turn_mismatch")
            except (ValueError, TimeoutError, EOFError):
                # Keep .sending as the durable unknown boundary. A later poll
                # checks native turn records with the exact client id and text.
                continue
            self._save_state(result_path, {**sent, "status": "accepted", "reconciled": False})
            self._seen.add(command_id)
            self._last_activity = time.monotonic()
            _emit({"type": "turn.steered", "thread_id": self.thread_id,
                   "turn_id": self.turn_id, "input_ref": command_id})

    def run(self):
        self._save_state(self.directory / "native-provider-environment.json", {
            name: os.environ[name] for name in (
                "CODEX_HOME", "CODEX_SQLITE_HOME", "HOME", "USERPROFILE", "XDG_CONFIG_HOME",
                "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME") if name in os.environ})
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=sys.stderr.buffer, bufsize=0)
        self._reader_stop.clear()
        reader = threading.Thread(target=self._reader, daemon=True)
        reader.start()
        aborted = False
        try:
            self.request("initialize", {"clientInfo": {"name": "meta_research", "version": "1.0"},
                "capabilities": {"experimentalApi": True}})
            self._send({"method": "initialized"})
            settings = {key: self.options[flag] for flag, key in (
                ("--model", "model"), ("-m", "model"), ("--cd", "cwd"), ("-C", "cwd"),
                ("--sandbox", "sandbox"), ("-s", "sandbox")) if flag in self.options}
            settings["approvalPolicy"] = "never"
            if "resume" in self.options:
                try:
                    result = self.request("thread/resume", {**settings, "threadId": self.options["resume"]})
                except NativeRpcError as error:
                    version = subprocess.run([self.command[0], "--version"], capture_output=True,
                        text=True, timeout=10)
                    rejected = {"method": error.method, "thread_id": self.options["resume"], "error": error.error,
                        "provider_version_output": version.stdout.strip() if version.returncode == 0 else None,
                        "provider_argv_hash": self.provider_argv_hash,
                        "supervisor_invocation_hash": read_supervisor_request(
                            self.directory / "supervisor-request.json", self.key).get("invocation_hash")
                            if (self.directory / "supervisor-request.json").is_file() else None}
                    self._save_state(self.directory / "native-resume-rejected.json", rejected)
                    _emit({"type": "meta_research.native_resume_rejected", **rejected})
                    raise
            else:
                result = self.request("thread/start", settings)
            thread = result.get("thread", {}) if isinstance(result, dict) else {}
            self.thread_id = thread.get("id")
            if not isinstance(self.thread_id, str) or not self.thread_id:
                raise ValueError("native_thread_identity_missing")
            if "resume" in self.options and self.thread_id != self.options["resume"]:
                raise ValueError("native_thread_identity_mismatch")
            _emit({"type": "thread.started", "thread_id": self.thread_id})
            if "resume" in self.options:
                _emit({"type": "thread.resumed", "thread_id": self.thread_id})
            input_id = "native_input_" + canonical_hash({"directory": str(self.directory), "prompt": self.prompt})
            parameters = {"threadId": self.thread_id, "clientUserMessageId": input_id,
                          "input": [{"type": "text", "text": self.prompt}]}
            if "--output-schema" in self.options:
                parameters["outputSchema"] = json.loads(Path(self.options["--output-schema"]).read_text())
            self._save_state(self.directory / "native-turn-start.json", {"thread_id": self.thread_id,
                "client_user_message_id": input_id, "parameters": parameters})
            result = self.request("turn/start", parameters)
            turn = result.get("turn", {}) if isinstance(result, dict) else {}
            if self.turn_id is None:
                self.turn_id = turn.get("id")
            if not isinstance(self.turn_id, str) or not self.turn_id:
                raise ValueError("native_turn_identity_missing")
            self._save_state(self.directory / "native-turn-id.json", {"thread_id": self.thread_id,
                "turn_id": self.turn_id, "client_user_message_id": input_id})
            self._last_activity = time.monotonic()
            while self.terminal is None:
                self.poll_guidance()
                try:
                    record = self._next()
                except queue.Empty:
                    record = None
                if record is not None:
                    self.notification(record)
                has_active_tool = bool(self._active_item_ids) or self._anonymous_active_items > 0
                effective_idle_timeout = (
                    self.turn_active_tool_idle_timeout_seconds
                    if has_active_tool and self.turn_active_tool_idle_timeout_seconds is not None
                    else self.turn_idle_timeout_seconds
                )
                if (
                    self.terminal is None
                    and effective_idle_timeout is not None
                    and (time.monotonic() - self._last_activity) >= effective_idle_timeout
                ):
                    watchdog_event = {
                        "type": "error",
                        "thread_id": self.thread_id,
                        "turn_id": self.turn_id,
                        "message": (
                            f"Native turn idle timeout after {effective_idle_timeout:.1f}s without activity"
                        ),
                        "error": {
                            "code": "native_turn_idle_timeout",
                            "idle_timeout_seconds": effective_idle_timeout,
                            "active_tool_in_flight": has_active_tool,
                        },
                        "will_retry": False,
                        "classification": "upstream_transient",
                    }
                    _emit(watchdog_event)
                    self.last_error = watchdog_event
                    self.last_error_classification = "upstream_transient"
                    self._interrupt_turn_best_effort()
                    if self.terminal is not None:
                        break
                    aborted = True
                    raise TimeoutError("native_turn_idle_timeout")
            success = self.terminal.get("status") == "completed"
            last_usage = self.usage.get("last") if isinstance(self.usage, dict) else None
            usage = ({"input_tokens": last_usage.get("inputTokens"),
                "cached_input_tokens": last_usage.get("cachedInputTokens"),
                "output_tokens": last_usage.get("outputTokens")}
                if isinstance(last_usage, dict) else None)
            terminal_error = self.terminal.get("error")
            if not success and terminal_error is None and isinstance(self.last_error, dict):
                terminal_error = self.last_error.get("error") or {"message": self.last_error.get("message")}
            if success:
                classification = None
            elif terminal_error is not None:
                direct = classify_native_error(terminal_error)
                if direct is not None:
                    classification = direct
                elif isinstance(self.last_error, dict) and terminal_error == self.last_error.get("error"):
                    classification = self.last_error_classification
                else:
                    classification = None
            else:
                classification = self.last_error_classification
            _emit({"type": "turn.completed" if success else "turn.failed",
                   "turn_id": self.turn_id, "error": terminal_error,
                   **({"classification": classification} if classification is not None else {}),
                   **({"usage": usage, "native_token_usage": self.usage} if usage is not None else {})})
            if success and "--output-last-message" in self.options:
                if not isinstance(self.last_message, str):
                    raise ValueError("native_final_message_missing")
                Path(self.options["--output-last-message"]).write_text(self.last_message, encoding="utf-8")
            return 0 if success else 1
        except ValueError as error:
            aborted = True
            if str(error) == "native_provider_stream_limit":
                _emit({
                    "type": "error",
                    "thread_id": self.thread_id,
                    "turn_id": self.turn_id,
                    "message": f"Native provider stdout exceeded {self.max_stdout_bytes} byte cap",
                    "error": {
                        "code": "native_provider_stream_limit",
                        "max_stdout_bytes": self.max_stdout_bytes,
                    },
                    "will_retry": False,
                })
            raise
        except BaseException:
            aborted = True
            raise
        finally:
            self._reader_stop.set()
            try:
                if self.process.stdin is not None and not self.process.stdin.closed:
                    self.process.stdin.close()
            except OSError:
                pass
            if aborted and self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)

    def read_original_turn(self):
        start = read_transport_envelope(self.directory / "native-turn-start.json", self.key)
        known_path = self.directory / "native-turn-id.json"
        known = read_transport_envelope(known_path, self.key) if known_path.exists() else None
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=sys.stderr.buffer, bufsize=0)
        self._reader_stop.clear()
        threading.Thread(target=self._reader, daemon=True).start()
        aborted = False
        try:
            self.request("initialize", {"clientInfo": {"name": "meta_research", "version": "1.0"},
                "capabilities": {"experimentalApi": True}})
            self._send({"method": "initialized"})
            # thread/read reads persisted rollout history without activating
            # the root or its required MCP servers. A metadata check must not
            # revive an old capability grant just to discover a terminal turn.
            response = self.request("thread/read", {"threadId": start["thread_id"], "includeTurns": True})
            thread = response.get("thread", {}) if isinstance(response, dict) else {}
            if thread.get("id") != start["thread_id"]:
                return False
            candidates = []
            for turn in thread.get("turns", []):
                if known is not None:
                    matches = turn.get("id") == known.get("turn_id")
                else:
                    matches = any(item.get("type") == "userMessage"
                        and item.get("clientId") == start["client_user_message_id"]
                        and any(part.get("type") == "text"
                            and part.get("text") == start["parameters"]["input"][0]["text"]
                            for part in item.get("content", []))
                        for item in turn.get("items", []))
                if matches:
                    candidates.append(turn)
            if len(candidates) != 1 or candidates[0].get("status") not in {"completed", "failed", "interrupted"}:
                return False
            turn = candidates[0]
            messages = [item.get("text") for item in turn.get("items", []) if item.get("type") == "agentMessage"]
            if turn["status"] == "completed" and (not messages or not isinstance(messages[-1], str)):
                return False
            self._save_state(self.directory / "native-reconciled.json", {
                "start_hash": canonical_hash(start), "thread_id": thread["id"],
                "turn": turn, "last_message": messages[-1] if messages else None})
            return True
        except BaseException:
            aborted = True
            raise
        finally:
            self._reader_stop.set()
            try:
                if self.process.stdin is not None and not self.process.stdin.closed:
                    self.process.stdin.close()
            except OSError:
                pass
            if aborted and self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)


def reconcile_native_operation(directory):
    """Read the original native turn; never start/resume a business turn.

    Completed turns return the exact message, native identity and public native
    item records. Failed/interrupted turns raise NativeTerminalTurnError with
    their signed actual outcome. Only a still unresolved turn returns None.
    The original supervisor spool and exit seal stay unchanged.
    """
    directory = Path(directory)
    start_path, reconciled_path = directory / "native-turn-start.json", directory / "native-reconciled.json"
    if not start_path.exists():
        return None
    _key_path, key = read_transport_key_for_operation(directory)
    start = read_transport_envelope(start_path, key)
    parameters = start.get("parameters")
    if (set(start) != {"thread_id", "client_user_message_id", "parameters"}
            or not isinstance(start.get("thread_id"), str) or not start["thread_id"]
            or not isinstance(parameters, dict) or parameters.get("threadId") != start["thread_id"]
            or parameters.get("clientUserMessageId") != start.get("client_user_message_id")
            or not isinstance(parameters.get("input"), list) or len(parameters["input"]) != 1
            or not isinstance(parameters["input"][0], dict) or parameters["input"][0].get("type") != "text"
            or not isinstance(parameters["input"][0].get("text"), str)
            or start.get("client_user_message_id") != "native_input_" + canonical_hash({
                "directory": str(directory), "prompt": parameters["input"][0]["text"]})):
        raise ValueError("native_reconciliation_start_invalid")
    original = read_transport_envelope(directory / "native-provider-argv.json", key)
    from meta_research.native_guidance import sealed_native_cli_version
    sealed_native_cli_version(original)
    if (not isinstance(original["argv"], list)
            or not all(isinstance(argument, str) for argument in original["argv"])):
        raise ValueError("native_provider_identity_invalid")
    _settings(original["argv"])
    if not reconciled_path.exists():
        environment = dict(os.environ)
        environment_path = directory / "native-provider-environment.json"
        if environment_path.exists():
            saved_environment = read_transport_envelope(environment_path, key)
            allowed = {"CODEX_HOME", "CODEX_SQLITE_HOME", "HOME", "USERPROFILE", "XDG_CONFIG_HOME",
                       "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"}
            if set(saved_environment) - allowed or any(not isinstance(value, str) for value in saved_environment.values()):
                raise ValueError("native_reconciliation_environment_invalid")
            environment.update(saved_environment)
        try:
            process = subprocess.run([sys.executable, "-m", __name__, "--provider-argv",
                str(directory / "native-provider-argv.json"), "--operation-directory", str(directory),
                "--reconcile-only"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment, timeout=65)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if process.returncode != 0 or not reconciled_path.exists():
            return None
    recovered = read_transport_envelope(reconciled_path, key)
    turn = recovered.get("turn")
    if (recovered.get("start_hash") != canonical_hash(start)
            or recovered.get("thread_id") != start.get("thread_id")
            or not isinstance(turn, dict) or turn.get("status") not in {"completed", "failed", "interrupted"}
            or not isinstance(turn.get("id"), str) or not turn["id"]
            or turn.get("error") is not None and not isinstance(turn["error"], dict)):
        raise ValueError("native_reconciliation_identity_invalid")
    status = turn["status"]
    if status == "completed" and not isinstance(recovered.get("last_message"), str):
        raise ValueError("native_reconciliation_message_invalid")
    known_path = directory / "native-turn-id.json"
    if known_path.exists():
        known = read_transport_envelope(known_path, key)
        if known.get("thread_id") != recovered["thread_id"] or known.get("turn_id") != turn["id"]:
            raise ValueError("native_reconciliation_identity_invalid")
    events = [{"type": "thread.started", "thread_id": recovered["thread_id"]}]
    for item in recovered["turn"].get("items", []):
        translated = _exec_item(item)
        if translated is not None:
            events.append({"type": "item.completed", "thread_id": recovered["thread_id"],
                "turn_id": recovered["turn"]["id"], "item": translated})
    reconciled_classification = None if status == "completed" else classify_native_error(turn.get("error"))
    events.append({"type": "turn.completed" if status == "completed" else "turn.failed",
        "turn_id": turn["id"], "error": turn.get("error"),
        **({"classification": reconciled_classification} if reconciled_classification is not None else {}),
        "native_reconciliation_ref": canonical_hash(recovered)})
    stdout = "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n"
    if status != "completed":
        request_path = directory / "supervisor-request.json"
        request = read_supervisor_request(request_path, key) if request_path.exists() else {}
        raise NativeTerminalTurnError(status=status, native_session_ref=recovered["thread_id"], stdout=stdout,
            classification=reconciled_classification,
            checkpoint={"schema_ref": "meta-research/provider-native-turn-terminal/v1",
                "supervisor_invocation_hash": request.get("invocation_hash"),
                "native_session_ref": recovered["thread_id"], "native_turn_ref": turn["id"],
                "native_turn_status": status, "native_error": turn.get("error"),
                "native_start_hash": canonical_hash(start), "native_reconciliation_hash": canonical_hash(recovered)})
    return recovered["last_message"], recovered["thread_id"], stdout
def _valid_supervised_result_destination(
    destination: Path,
    directory: Path,
    key: bytes,
    *,
    request: dict[str, object] | None = None,
) -> bool:
    original = directory / "last-message.json"
    if destination in {original, directory / ".last-message.supervisor.tmp"}:
        return True
    pipe = re.fullmatch(r"/proc/([0-9]+)/fd/([0-9]+)", str(destination))
    if pipe is not None:
        ready = read_transport_envelope(directory / "supervisor-ready.json", key)
        if request is None:
            request = read_supervisor_request(directory / "supervisor-request.json", key)
        return (
            ready.get("supervisor_process_id") == int(pipe[1])
            and ready.get("invocation_hash") == request.get("invocation_hash")
            and int(pipe[2]) >= 3
            and stat.S_ISFIFO(destination.stat().st_mode)
        )
    if (
        destination.name == "result"
        and destination.parent.name.startswith((".result-pipe-", ".recorder-pipe-"))
        and destination.parent.parent.resolve() == directory.resolve()
    ):
        ready = read_transport_envelope(directory / "supervisor-ready.json", key)
        if request is None:
            request = read_supervisor_request(directory / "supervisor-request.json", key)
        if ready.get("invocation_hash") != request.get("invocation_hash"):
            return False
        try:
            parent_stat = destination.parent.lstat()
            fifo_stat = destination.lstat()
        except OSError:
            return False
        return (
            stat.S_ISDIR(parent_stat.st_mode)
            and stat.S_IMODE(parent_stat.st_mode) == 0o700
            and stat.S_ISFIFO(fifo_stat.st_mode)
            and stat.S_IMODE(fifo_stat.st_mode) == 0o600
        )
    return False


def _strict_exec(argv, args, key):
    """Preserve exec policy; attest only its exact, output-free resume rejection."""
    if (len(argv) < 3 or argv[1] != "exec" or argv.count("--output-last-message") != 1
        or argv.count("--output-schema") != 1):
        raise ValueError("native_codex_exec_arguments_invalid")
    directory = args.operation_directory
    original_result = argv[argv.index("--output-last-message") + 1]
    if (original_result != str(directory / "last-message.json")
        or args.output_schema is None
        or str(args.output_schema) != argv[argv.index("--output-schema") + 1]
        or args.output_last_message is None):
        raise ValueError("native_result_path_conflict")
    destination = args.output_last_message
    request = read_supervisor_request(directory / "supervisor-request.json", key)
    if not _valid_supervised_result_destination(destination, directory, key, request=request):
        raise ValueError("native_result_path_conflict")
    command = list(argv)
    command[command.index("--output-last-message") + 1] = str(destination)
    # Inherit stdin/cwd/environment and the supervisor's provider process group.
    # Do not consume/rewrite the prompt or translate any exec permission option.
    process = None
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
        # The supervisor signals our whole group. Stay alive long enough to
        # reap the exec child, instead of leaving it orphaned during shutdown.
        if process is not None and process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if stopped:
        return 70
    process = subprocess.Popen(command, stdin=sys.stdin.buffer, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0)
    if stopped and process.poll() is None:
        process.terminate()
    stderr = bytearray()
    stdout_bytes = 0
    max_stdout_bytes = _env_positive_int("META_RESEARCH_NATIVE_MAX_STDOUT_BYTES", MAX_STDOUT_BYTES)
    complete = [False, False]
    errors = []
    stderr_overflow = False
    stdout_overflow = False

    def drain(stream, index):
        nonlocal stdout_bytes, stderr_overflow, stdout_overflow
        try:
            while chunk := stream.read(65536):
                if index == 0:
                    if stdout_overflow:
                        continue
                    remaining = max_stdout_bytes - stdout_bytes
                    if len(chunk) > remaining:
                        if remaining > 0:
                            sys.stdout.buffer.write(chunk[:remaining])
                            sys.stdout.buffer.flush()
                            stdout_bytes += remaining
                        stdout_overflow = True
                        errors.append(ValueError("native_provider_stream_limit"))
                        if process is not None and process.poll() is None:
                            process.terminate()
                        continue
                    stdout_bytes += len(chunk)
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                elif len(stderr) + len(chunk) <= 4096 and not stderr_overflow:
                    stderr.extend(chunk)
                else:
                    # Drain the rest without retaining it; overflow is never
                    # proof and must not deadlock the provider on its stderr.
                    stderr_overflow = True
            complete[index] = True
        except (OSError, ValueError) as error:
            errors.append(error)

    drainers = [threading.Thread(target=drain, args=(stream, index), daemon=True)
                for index, stream in enumerate((process.stdout, process.stderr))]
    for drainer in drainers:
        drainer.start()
    while True:
        try:
            returncode = process.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if stopped or stdout_overflow:
                if process.poll() is None:
                    process.terminate()
                try:
                    returncode = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    returncode = process.wait(timeout=5)
                break
    for drainer in drainers:
        drainer.join(timeout=1)
    if stopped or errors or not all(complete):
        # The outer supervisor owns descendant cleanup and the signed terminal
        # reason. Incomplete pipes cannot authorize a replacement.
        return 70
    native = argv[-2] if argv[-3] == "resume" and argv[-1] == "-" else None
    expected = ("Error: thread/resume: thread/resume failed: no rollout found for thread id "
                + str(native) + " (code -32600)\n").encode("utf-8")
    if returncode != 1 or native is None or stdout_bytes or stderr_overflow or bytes(stderr) != expected:
        return returncode
    version = subprocess.run([command[0], "--version"], capture_output=True, text=True, timeout=10)
    from meta_research.native_guidance import sealed_native_cli_version
    launch = read_transport_envelope(directory / "native-provider-argv.json", key)
    if stopped or version.returncode != 0 or version.stdout.strip() != "codex-cli " + sealed_native_cli_version(launch):
        return returncode
    proof = {"source": "exec", "method": "thread/resume", "thread_id": native,
        "error": {"code": -32600, "message": "no rollout found for thread id " + native},
        "exec_returncode": returncode, "exec_stdout_bytes": stdout_bytes,
        "exec_stderr": stderr.decode("utf-8"), "provider_version_output": version.stdout.strip(),
        "provider_argv_hash": canonical_hash(argv),
        "supervisor_invocation_hash": request["invocation_hash"]}
    # This is candidate evidence, not recovery authorization. The adapter also
    # requires the final supervisor receipt and its empty result-file hash.
    write_transport_envelope(directory / "native-resume-rejected.json", proof, key)
    _emit({"type": "meta_research.native_resume_rejected", **proof})
    return 70


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-argv", required=True, type=Path)
    parser.add_argument("--operation-directory", required=True, type=Path)
    parser.add_argument("--output-schema", type=Path)
    parser.add_argument("--output-last-message", type=Path)
    parser.add_argument("prompt_source", nargs="?", choices=("-",))
    parser.add_argument("--reconcile-only", action="store_true")
    parser.add_argument("--passthrough-exec", action="store_true")
    args = parser.parse_args()
    try:
        _key_path, key = read_transport_key_for_operation(args.operation_directory)
        launch = read_transport_envelope(args.provider_argv, key)
        from meta_research.native_guidance import sealed_native_cli_version
        from meta_research.codex_runtime import CODEX_LOCKED_VERSION, DEFAULT_CODEX_CLI_VERSION
        version = sealed_native_cli_version(launch)
        argv = launch["argv"]
        if version != DEFAULT_CODEX_CLI_VERSION or CODEX_LOCKED_VERSION != DEFAULT_CODEX_CLI_VERSION:
            observed = subprocess.run([argv[0], "--version"], capture_output=True, text=True, timeout=10)
            if observed.returncode != 0 or observed.stdout.strip() != f"codex-cli {version}":
                raise ValueError("native_provider_version_mismatch")
        if args.passthrough_exec:
            if args.reconcile_only:
                raise ValueError("native_exec_reconcile_mode_invalid")
            return _strict_exec(argv, args, key)
        bridge = NativeCodexBridge(argv, args.operation_directory, "" if args.reconcile_only else sys.stdin.read())
        if args.output_schema is not None and str(args.output_schema) != bridge.options.get("--output-schema"):
            raise ValueError("native_schema_path_conflict")
        if args.output_last_message is not None:
            # The mature supervisor redirects its bound result to this private
            # FIFO or temporary file, then seals/publishes it. Honor that
            # redirection; writing the original path would be replaced by its
            # empty temp.
            original = args.operation_directory / "last-message.json"
            valid_destination = _valid_supervised_result_destination(
                args.output_last_message, args.operation_directory, key
            )
            if bridge.options.get("--output-last-message") != str(original) or not valid_destination:
                raise ValueError("native_result_path_conflict")
            bridge.options["--output-last-message"] = str(args.output_last_message)
        return (0 if bridge.read_original_turn() else 75) if args.reconcile_only else bridge.run()
    except Exception as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr, flush=True)
        if (args.operation_directory / "native-turn-start.json").is_file():
            _emit({"type": "meta_research.native_outcome_unknown"})
            return 75
        return 70


if __name__ == "__main__":
    exit_code = main()
    if os.name == "posix" and exit_code < 0:
        native_signal = -exit_code
        if native_signal not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(native_signal, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {native_signal})
        os.kill(os.getpid(), native_signal)
    raise SystemExit(exit_code)
