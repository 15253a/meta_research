"""Codex 0.159 app-server transport inside the existing durable supervisor.

Only native thread/start, thread/resume, turn/start and turn/steer create
provider inputs. A prepared or uncertain steer is never replayed as a new turn.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import stat
import subprocess
import sys
import threading
import time
from meta_research.owners.common import canonical_hash

from meta_research.provider_supervisor import (
    read_transport_key_for_operation, read_transport_envelope, write_transport_envelope, read_supervisor_request,
)


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
             "imageView": "image_view", "contextCompaction": "context_compaction"}
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
    def __init__(self, *, status, native_session_ref, stdout, checkpoint):
        super().__init__("provider_native_turn_" + status)
        self.status = status
        self.native_session_ref = native_session_ref
        self.stdout = stdout
        self.checkpoint = checkpoint


class NativeCodexBridge:
    def __init__(self, argv, directory, prompt):
        self.directory = Path(directory)
        self.key_path, self.key = read_transport_key_for_operation(self.directory)
        self.command, self.options = _settings(argv)
        self.provider_argv_hash = canonical_hash(argv)
        self.prompt = prompt
        self.thread_id = self.turn_id = None
        self.terminal = None
        self.last_message = None
        self._next_id = 0
        self._records = queue.Queue(maxsize=64)
        self._seen = set()
        self._next_reconcile = {}
        self.process = None
        self.usage = None

    def _send(self, value):
        self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def _reader(self):
        try:
            total = 0
            while line := self.process.stdout.readline(64 * 1024 * 1024 + 1):
                total += len(line)
                if total > 64 * 1024 * 1024:
                    raise ValueError("native_provider_stream_limit")
                self._records.put(json.loads(line))
        except Exception as error:
            self._records.put(error)
        finally:
            self._records.put(EOFError("native_provider_stream_closed"))

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
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if isinstance(item, dict) and (translated := _exec_item(item)) is not None:
                _emit({"type": "item.started" if method.endswith("started") else "item.completed",
                       "thread_id": params.get("threadId"), "turn_id": params.get("turnId"),
                       "item": translated})
                if method == "item/completed" and item.get("type") == "agentMessage":
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
            if path.stem in self._seen:
                continue
            if closed.exists():
                read_transport_envelope(closed, self.key)
                return
            command_id = command.get("id")
            if command_id != path.stem or not isinstance(command.get("message"), dict):
                raise ValueError("native_guidance_command_invalid")
            text = "正式人类指导变化（保留此前工作历史）：\n" + json.dumps(command["message"], ensure_ascii=False)
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
                if sent != {"id": command_id, "thread_id": self.thread_id,
                            "turn_id": self.turn_id, "text": text}:
                    raise ValueError("native_guidance_send_identity_invalid")
                observed = self._observed_input(command_id, text)
                if observed:
                    self._save_state(result_path, {**sent, "status": "accepted", "reconciled": True})
                    self._seen.add(command_id)
                # Absence from a live or partial history is no proof of failed
                # delivery. Keep the original send unresolved; never retransmit.
                continue
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
            _emit({"type": "turn.steered", "thread_id": self.thread_id,
                   "turn_id": self.turn_id, "input_ref": command_id})

    def run(self):
        self._save_state(self.directory / "native-provider-environment.json", {
            name: os.environ[name] for name in (
                "CODEX_HOME", "CODEX_SQLITE_HOME", "HOME", "USERPROFILE", "XDG_CONFIG_HOME",
                "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME") if name in os.environ})
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=sys.stderr.buffer, bufsize=0)
        reader = threading.Thread(target=self._reader, daemon=True)
        reader.start()
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
            while self.terminal is None:
                self.poll_guidance()
                try:
                    self.notification(self._next())
                except queue.Empty:
                    continue
            success = self.terminal.get("status") == "completed"
            last_usage = self.usage.get("last") if isinstance(self.usage, dict) else None
            usage = ({"input_tokens": last_usage.get("inputTokens"),
                "cached_input_tokens": last_usage.get("cachedInputTokens"),
                "output_tokens": last_usage.get("outputTokens")}
                if isinstance(last_usage, dict) else None)
            _emit({"type": "turn.completed" if success else "turn.failed",
                   "turn_id": self.turn_id, "error": self.terminal.get("error"),
                   **({"usage": usage, "native_token_usage": self.usage} if usage is not None else {})})
            if success and "--output-last-message" in self.options:
                if not isinstance(self.last_message, str):
                    raise ValueError("native_final_message_missing")
                Path(self.options["--output-last-message"]).write_text(self.last_message, encoding="utf-8")
            return 0 if success else 1
        finally:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)

    def read_original_turn(self):
        start = read_transport_envelope(self.directory / "native-turn-start.json", self.key)
        known_path = self.directory / "native-turn-id.json"
        known = read_transport_envelope(known_path, self.key) if known_path.exists() else None
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=sys.stderr.buffer, bufsize=0)
        threading.Thread(target=self._reader, daemon=True).start()
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
        finally:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate(); self.process.wait(timeout=5)


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
    if (set(original) != {"argv"} or not isinstance(original["argv"], list)
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
    events.append({"type": "turn.completed" if status == "completed" else "turn.failed",
        "turn_id": turn["id"], "error": turn.get("error"),
        "native_reconciliation_ref": canonical_hash(recovered)})
    stdout = "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n"
    if status != "completed":
        request_path = directory / "supervisor-request.json"
        request = read_supervisor_request(request_path, key) if request_path.exists() else {}
        raise NativeTerminalTurnError(status=status, native_session_ref=recovered["thread_id"], stdout=stdout,
            checkpoint={"schema_ref": "meta-research/provider-native-turn-terminal/v1",
                "supervisor_invocation_hash": request.get("invocation_hash"),
                "native_session_ref": recovered["thread_id"], "native_turn_ref": turn["id"],
                "native_turn_status": status, "native_error": turn.get("error"),
                "native_start_hash": canonical_hash(start), "native_reconciliation_hash": canonical_hash(recovered)})
    return recovered["last_message"], recovered["thread_id"], stdout
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-argv", required=True, type=Path)
    parser.add_argument("--operation-directory", required=True, type=Path)
    parser.add_argument("--output-schema", type=Path)
    parser.add_argument("--output-last-message", type=Path)
    parser.add_argument("prompt_source", nargs="?", choices=("-",))
    parser.add_argument("--reconcile-only", action="store_true")
    args = parser.parse_args()
    try:
        _key_path, key = read_transport_key_for_operation(args.operation_directory)
        argv = read_transport_envelope(args.provider_argv, key)["argv"]
        bridge = NativeCodexBridge(argv, args.operation_directory, "" if args.reconcile_only else sys.stdin.read())
        if args.output_schema is not None and str(args.output_schema) != bridge.options.get("--output-schema"):
            raise ValueError("native_schema_path_conflict")
        if args.output_last_message is not None:
            # The mature supervisor redirects its bound result to this private
            # temporary file, then seals/publishes it. Honor that redirection;
            # writing the original path would be replaced by its empty temp.
            original = args.operation_directory / "last-message.json"
            allowed = {original, args.operation_directory / ".last-message.supervisor.tmp"}
            valid_destination = args.output_last_message in allowed
            pipe = re.fullmatch(r"/proc/([0-9]+)/fd/([0-9]+)", str(args.output_last_message))
            if pipe is not None:
                ready = read_transport_envelope(args.operation_directory / "supervisor-ready.json", key)
                request = read_supervisor_request(args.operation_directory / "supervisor-request.json", key)
                valid_destination = (ready.get("supervisor_process_id") == int(pipe[1])
                    and ready.get("invocation_hash") == request.get("invocation_hash")
                    and int(pipe[2]) >= 3 and stat.S_ISFIFO(args.output_last_message.stat().st_mode))
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
    raise SystemExit(main())
