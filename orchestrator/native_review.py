"""Parent-owned verification of native Codex child-review activity.

The ledger consumes only the app-server bytes captured from the current
supervised runner call.  It never opens a caller-supplied event path and never
accepts parent prose as evidence that a child reviewer existed.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_READ_PREFIX = "native-review-read:"
_DEFAULT_MAX_LINE_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_STREAM_BYTES = 32 * 1024 * 1024
RAW_SPAWN_PROOF_MODE = "raw-spawn-v1"
RESUMED_LINEAGE_PROOF_MODE = "appserver-resume-lineage-v1"
_SPAWN_PROOF_MODES = {
    RAW_SPAWN_PROOF_MODE,
    RESUMED_LINEAGE_PROOF_MODE,
}


class NativeReviewError(RuntimeError):
    """The owner event stream cannot prove a native child-review chain."""


@dataclass(frozen=True)
class NativeReviewInputEvidence:
    item_id: str
    child_turn_id: str
    review_request_id: str
    reviewer_brief_hash: str
    candidate_manifest_hash: str


@dataclass(frozen=True)
class NativeReviewEvidence:
    parent_thread_id: str
    parent_turn_id: str
    call_id: str
    child_thread_id: str
    child_turn_id: str
    final_bytes: bytes
    review_input: Optional[NativeReviewInputEvidence] = None


@dataclass(frozen=True)
class NativeReviewExecutionEvidence:
    """Guardian-bound replay of every child observed in one resident turn."""

    runner_call_id: int
    cycle_id: str
    stage: str
    purpose: str
    execution_receipt_ref: str
    execution_operation_id: str
    capture_stdout_sha256: str
    children: tuple[NativeReviewEvidence, ...]
    spawn_proof_mode: str = RAW_SPAWN_PROOF_MODE


@dataclass
class _ChildState:
    call_id: str
    child_thread_id: str
    spawn_order: int
    child_turn_id: Optional[str] = None
    final_item_id: Optional[str] = None
    final_bytes: Optional[bytes] = None
    terminal: bool = False
    read_verified: bool = False
    review_input: Optional[NativeReviewInputEvidence] = None


def _strict_object(raw: bytes) -> dict[str, Any]:
    def unique(pairs):  # noqa: ANN001 - json hook protocol
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise NativeReviewError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        text = raw.decode("utf-8", "strict")
        value = json.loads(
            text, object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                NativeReviewError(f"invalid JSON constant: {token}")))
    except NativeReviewError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NativeReviewError(
            f"malformed app-server JSONL: {type(error).__name__}") from error
    if not isinstance(value, dict):
        raise NativeReviewError("app-server JSONL event must be an object")
    return value


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise NativeReviewError(f"invalid {label}")
    return value


class NativeReviewLedger:
    """Bounded incremental verifier for one app-server parent turn."""

    def __init__(
            self, *, max_line_bytes: int = _DEFAULT_MAX_LINE_BYTES,
            max_stream_bytes: int = _DEFAULT_MAX_STREAM_BYTES,
            spawn_proof_mode: str = RAW_SPAWN_PROOF_MODE):
        if (isinstance(max_line_bytes, bool)
                or not isinstance(max_line_bytes, int) or max_line_bytes <= 0):
            raise ValueError("max_line_bytes must be a positive integer")
        if (isinstance(max_stream_bytes, bool)
                or not isinstance(max_stream_bytes, int)
                or max_stream_bytes < max_line_bytes):
            raise ValueError(
                "max_stream_bytes must be an integer >= max_line_bytes")
        if spawn_proof_mode not in _SPAWN_PROOF_MODES:
            raise ValueError("unsupported native review spawn proof mode")
        self.max_line_bytes = max_line_bytes
        self.max_stream_bytes = max_stream_bytes
        self.spawn_proof_mode = spawn_proof_mode
        self._lock = threading.RLock()
        self._raw = bytearray()
        self._pending = bytearray()
        self._finalized = False
        self._parent_thread_id: Optional[str] = None
        self._parent_turn_id: Optional[str] = None
        self._parent_terminal = False
        self._spawns: dict[str, int] = {}
        self._children_by_call: dict[str, _ChildState] = {}
        self._children_by_thread: dict[str, _ChildState] = {}
        self._child_claims: dict[str, str] = {}
        self._request_claims: dict[str, str] = {}
        self._poisoned_claims: set[str] = set()

    def feed(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise TypeError("native review ledger accepts bytes")
        with self._lock:
            if self._finalized:
                raise NativeReviewError("ledger is already finalized")
            if len(self._raw) + len(chunk) > self.max_stream_bytes:
                raise NativeReviewError("app-server stream exceeds stream limit")
            self._raw.extend(chunk)
            self._pending.extend(chunk)
            while True:
                newline = self._pending.find(b"\n")
                if newline < 0:
                    if len(self._pending) > self.max_line_bytes:
                        raise NativeReviewError(
                            "app-server JSONL line exceeds line limit")
                    break
                if newline > self.max_line_bytes:
                    raise NativeReviewError(
                        "app-server JSONL line exceeds line limit")
                line = bytes(self._pending[:newline])
                del self._pending[:newline + 1]
                if not line:
                    raise NativeReviewError("empty app-server JSONL line")
                self._observe(_strict_object(line))

    def _observe(self, event: dict[str, Any]) -> None:
        response_id = event.get("id")
        result = event.get("result")
        if response_id == 1 and isinstance(result, dict):
            self._bind_parent(result)
            return
        if response_id == 2 and isinstance(result, dict):
            self._bind_parent_turn(result)
            return
        if (isinstance(response_id, str)
                and response_id.startswith(_READ_PREFIX)):
            self._verify_child_read(response_id, result)
            return

        method = event.get("method")
        params = event.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        item = params.get("item")
        if method == "rawResponseItem/completed" and isinstance(item, dict):
            if (item.get("type") == "function_call"
                    and item.get("namespace") == "collaboration"
                    and item.get("name") == "spawn_agent"):
                self._observe_spawn(params, item)
            return
        if method == "item/completed" and isinstance(item, dict):
            if (item.get("type") == "subAgentActivity"
                    and item.get("kind") == "started"):
                self._observe_activity(params, item)
            elif item.get("type") == "mcpToolCall":
                self._observe_mcp_tool_call(params, item)
            elif item.get("type") == "agentMessage":
                self._observe_agent_message(params, item)
            return
        if method == "turn/completed":
            self._observe_terminal(params)

    def _bind_parent(self, result: dict[str, Any]) -> None:
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("parentThreadId") is not None:
            raise NativeReviewError("invalid parent thread response")
        parent = _identity(thread.get("id"), "parent thread id")
        if self._parent_thread_id is not None:
            raise NativeReviewError("duplicate parent thread response")
        self._parent_thread_id = parent

    def _bind_parent_turn(self, result: dict[str, Any]) -> None:
        if self._parent_thread_id is None:
            raise NativeReviewError("parent turn arrived before parent thread")
        turn = result.get("turn")
        if not isinstance(turn, dict) or turn.get("status") != "inProgress":
            raise NativeReviewError("invalid parent turn response")
        turn_id = _identity(turn.get("id"), "parent turn id")
        if self._parent_turn_id is not None:
            raise NativeReviewError("duplicate parent turn response")
        self._parent_turn_id = turn_id

    def _require_parent_event(self, params: dict[str, Any]) -> None:
        if self._parent_thread_id is None or self._parent_turn_id is None:
            raise NativeReviewError("native event arrived before parent binding")
        if params.get("threadId") != self._parent_thread_id:
            raise NativeReviewError("native event has wrong parent thread")
        if params.get("turnId") != self._parent_turn_id:
            raise NativeReviewError("native event has wrong parent turn")

    def _observe_spawn(
            self, params: dict[str, Any], item: dict[str, Any]) -> None:
        self._require_parent_event(params)
        raw_arguments = item.get("arguments")
        if not isinstance(raw_arguments, str):
            raise NativeReviewError("spawn arguments are missing")
        arguments = _strict_object(raw_arguments.encode("utf-8"))
        if set(arguments) != {"task_name", "fork_turns", "message"}:
            raise NativeReviewError(
                "spawn arguments must contain task_name/fork_turns/message")
        if arguments.get("fork_turns") != "none":
            raise NativeReviewError(
                "native reviewer spawn requires fork_turns='none'")
        _identity(arguments.get("task_name"), "spawn task name")
        message = arguments.get("message")
        if (not isinstance(message, str) or not message
                or len(message.encode("utf-8")) > self.max_line_bytes):
            raise NativeReviewError("spawn encrypted message is invalid")
        call_id = _identity(item.get("call_id"), "spawn call id")
        if call_id in self._spawns:
            raise NativeReviewError("duplicate native spawn call")
        self._spawns[call_id] = len(self._spawns)

    def _observe_activity(
            self, params: dict[str, Any], item: dict[str, Any]) -> None:
        self._require_parent_event(params)
        call_id = _identity(item.get("id"), "activity call id")
        child_id = _identity(item.get("agentThreadId"), "child thread id")
        if call_id not in self._spawns:
            if self.spawn_proof_mode != RESUMED_LINEAGE_PROOF_MODE:
                raise NativeReviewError(
                    "child activity has no linked raw spawn")
            # ``thread/resume`` cannot opt into experimentalRawEvents.  The
            # normalized subAgentActivity is still emitted by app-server and
            # is later bound to an authoritative child ``thread/read`` result.
            # Only the runner-selected resume mode may use that lineage proof.
            self._spawns[call_id] = len(self._spawns)
        if call_id in self._children_by_call:
            raise NativeReviewError("duplicate child activity binding")
        if child_id == self._parent_thread_id or child_id in self._children_by_thread:
            raise NativeReviewError("conflicting child thread binding")
        child = _ChildState(
            call_id=call_id, child_thread_id=child_id,
            spawn_order=self._spawns[call_id])
        self._children_by_call[call_id] = child
        self._children_by_thread[child_id] = child

    def _observe_mcp_tool_call(
            self, params: dict[str, Any], item: dict[str, Any]) -> None:
        if (item.get("server") != "meta_research_runtime"
                or item.get("tool") != "read_review_input"):
            return
        child = self._children_by_thread.get(params.get("threadId"))
        if child is None:
            # A parent call can obtain the same owner response, but it is not
            # evidence that the clean reviewer child received that input.
            return
        if child.final_bytes is not None or child.terminal:
            raise NativeReviewError(
                "review input MCP call arrived after child final")
        if child.review_input is not None:
            raise NativeReviewError("duplicate child review input MCP call")
        turn_id = _identity(params.get("turnId"), "review input child turn id")
        item_id = _identity(item.get("id"), "review input MCP item id")
        arguments = item.get("arguments")
        if (not isinstance(arguments, dict)
                or set(arguments) != {"review_request_id"}):
            raise NativeReviewError("review input MCP arguments are invalid")
        request_id = _identity(
            arguments.get("review_request_id"), "review input request id")
        result = item.get("result")
        if (item.get("status") != "completed" or item.get("error") is not None
                or not isinstance(result, dict)):
            raise NativeReviewError("review input MCP call did not succeed")
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise NativeReviewError(
                "review input MCP structured result is missing")
        if (structured.get("ok") is not True
                or structured.get("protocol") != "native-review-input-v1"
                or structured.get("review_request_id") != request_id):
            raise NativeReviewError(
                "review input MCP result/request identity mismatch")
        brief_hash = structured.get("reviewer_brief_hash")
        manifest_hash = structured.get("candidate_manifest_hash")
        sha_re = re.compile(r"^sha256:[0-9a-f]{64}$")
        if (not isinstance(brief_hash, str)
                or sha_re.fullmatch(brief_hash) is None
                or not isinstance(manifest_hash, str)
                or sha_re.fullmatch(manifest_hash) is None):
            raise NativeReviewError("review input MCP hashes are invalid")
        child.review_input = NativeReviewInputEvidence(
            item_id=item_id,
            child_turn_id=turn_id,
            review_request_id=request_id,
            reviewer_brief_hash=brief_hash,
            candidate_manifest_hash=manifest_hash)

    def _observe_agent_message(
            self, params: dict[str, Any], item: dict[str, Any]) -> None:
        child = self._children_by_thread.get(params.get("threadId"))
        if child is None:
            return
        if item.get("phase") != "final_answer":
            return
        turn_id = _identity(params.get("turnId"), "child turn id")
        item_id = _identity(item.get("id"), "child final item id")
        text = item.get("text")
        if not isinstance(text, str):
            raise NativeReviewError("child final text is missing")
        payload = text.encode("utf-8")
        if len(payload) > self.max_line_bytes:
            raise NativeReviewError("child final exceeds line limit")
        if child.final_bytes is not None:
            raise NativeReviewError("duplicate child final answer")
        if (child.review_input is not None
                and child.review_input.child_turn_id != turn_id):
            raise NativeReviewError(
                "review input MCP call and child final use different turns")
        child.child_turn_id = turn_id
        child.final_item_id = item_id
        child.final_bytes = payload

    def _observe_terminal(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn = params.get("turn")
        if not isinstance(turn, dict):
            raise NativeReviewError("turn/completed payload is malformed")
        turn_id = _identity(turn.get("id"), "terminal turn id")
        status = turn.get("status")
        if "error" not in turn or turn.get("error") is not None:
            raise NativeReviewError(
                "turn/completed must carry an explicit null error")
        if thread_id == self._parent_thread_id:
            if turn_id != self._parent_turn_id or status != "completed":
                raise NativeReviewError("parent turn is not terminal completed")
            if self._parent_terminal:
                raise NativeReviewError("duplicate parent terminal")
            self._parent_terminal = True
            return
        child = self._children_by_thread.get(thread_id)
        if child is None:
            return
        if status != "completed":
            raise NativeReviewError("child terminal status is not completed")
        if child.final_bytes is None or child.child_turn_id != turn_id:
            raise NativeReviewError(
                "child terminal arrived without ordered final answer")
        if child.terminal:
            raise NativeReviewError("duplicate child terminal")
        child.terminal = True

    def _verify_child_read(self, response_id: str, result: Any) -> None:
        child_id = response_id[len(_READ_PREFIX):]
        child = self._children_by_thread.get(child_id)
        if child is None:
            raise NativeReviewError("thread/read refers to an unknown child")
        if not child.terminal or child.final_bytes is None:
            raise NativeReviewError("thread/read arrived before child terminal")
        if child.read_verified:
            raise NativeReviewError("duplicate child thread/read response")
        if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
            raise NativeReviewError("child thread/read result is missing")
        thread = result["thread"]
        if thread.get("id") != child_id:
            raise NativeReviewError("thread/read child identity mismatch")
        if thread.get("parentThreadId") != self._parent_thread_id:
            raise NativeReviewError("thread/read parent identity mismatch")
        source = thread.get("source")
        spawn_source = (
            source.get("subAgent", {}).get("thread_spawn")
            if isinstance(source, dict)
            and isinstance(source.get("subAgent"), dict) else None)
        if (not isinstance(spawn_source, dict)
                or spawn_source.get("parent_thread_id")
                != self._parent_thread_id):
            raise NativeReviewError("thread/read subagent parent mismatch")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise NativeReviewError("thread/read turns are missing")
        matching_turns = [
            turn for turn in turns
            if isinstance(turn, dict)
            and turn.get("id") == child.child_turn_id
            and turn.get("status") == "completed"
            and "error" in turn
            and turn.get("error") is None]
        if len(matching_turns) != 1:
            raise NativeReviewError(
                "thread/read child terminal/error mismatch")
        items = matching_turns[0].get("items")
        if not isinstance(items, list):
            raise NativeReviewError("thread/read child items are missing")
        matching_finals = [
            item for item in items
            if isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and item.get("phase") == "final_answer"
            and isinstance(item.get("text"), str)
            and item["text"].encode("utf-8") == child.final_bytes]
        if len(matching_finals) != 1:
            raise NativeReviewError("thread/read authoritative final mismatch")
        if child.review_input is not None:
            matching_inputs = []
            for item in items:
                if (not isinstance(item, dict)
                        or item.get("type") != "mcpToolCall"
                        or item.get("id") != child.review_input.item_id
                        or item.get("server") != "meta_research_runtime"
                        or item.get("tool") != "read_review_input"
                        or item.get("status") != "completed"
                        or item.get("error") is not None
                        or item.get("arguments") != {
                            "review_request_id":
                            child.review_input.review_request_id}):
                    continue
                result_item = item.get("result")
                structured = (
                    result_item.get("structuredContent")
                    if isinstance(result_item, dict) else None)
                if (not isinstance(structured, dict)
                        or structured.get("ok") is not True
                        or structured.get("protocol")
                        != "native-review-input-v1"
                        or structured.get("review_request_id")
                        != child.review_input.review_request_id
                        or structured.get("reviewer_brief_hash")
                        != child.review_input.reviewer_brief_hash
                        or structured.get("candidate_manifest_hash")
                        != child.review_input.candidate_manifest_hash):
                    continue
                matching_inputs.append(item)
            if len(matching_inputs) != 1:
                raise NativeReviewError(
                    "thread/read authoritative review input mismatch")
        child.read_verified = True
        self._refresh_child_claim_poison(child)

    @staticmethod
    def _embedded_review_request_id(child: _ChildState) -> Optional[str]:
        if child.final_bytes is None or not child.read_verified:
            return None
        try:
            payload = _strict_object(child.final_bytes)
        except NativeReviewError:
            return None
        request_id = payload.get("review_request_id")
        if not isinstance(request_id, str) or _ID_RE.fullmatch(request_id) is None:
            return None
        return request_id

    @classmethod
    def _claimed_review_request_id(
            cls, child: _ChildState) -> Optional[str]:
        """Prefer the owner-delivered request over untrusted child final text."""
        if not child.read_verified:
            return None
        if child.review_input is not None:
            return child.review_input.review_request_id
        return cls._embedded_review_request_id(child)

    def _refresh_child_claim_poison(self, child: _ChildState) -> None:
        request_id = self._claimed_review_request_id(child)
        if request_id is None:
            return
        claimed_child = self._request_claims.get(request_id)
        if claimed_child is not None and claimed_child != child.child_thread_id:
            self._poisoned_claims.add(request_id)

    def _refresh_claim_poison(self, claim_id: str) -> None:
        claimed_child = self._request_claims.get(claim_id)
        if claimed_child is None:
            return
        for child in self._children_by_thread.values():
            if (child.child_thread_id != claimed_child
                    and self._claimed_review_request_id(child) == claim_id):
                self._poisoned_claims.add(claim_id)
                return

    def parent_identity(self) -> tuple[str, str]:
        """Return the trusted current parent identity once app-server binds it."""
        with self._lock:
            if self._parent_thread_id is None or self._parent_turn_id is None:
                raise NativeReviewError("parent identity is missing")
            return self._parent_thread_id, self._parent_turn_id

    def completed_child(self, child_thread_id: str) -> NativeReviewEvidence:
        """Read one complete child chain without ending the live parent turn."""
        child_id = _identity(child_thread_id, "child thread id")
        with self._lock:
            child = self._children_by_thread.get(child_id)
            if child is None:
                raise NativeReviewError("native child does not exist")
            if child.final_bytes is None:
                raise NativeReviewError("child final answer is missing")
            if not child.terminal:
                raise NativeReviewError("child terminal event is missing")
            if not child.read_verified:
                raise NativeReviewError("child thread/read proof is missing")
            assert self._parent_thread_id is not None
            assert self._parent_turn_id is not None
            assert child.child_turn_id is not None
            return NativeReviewEvidence(
                parent_thread_id=self._parent_thread_id,
                parent_turn_id=self._parent_turn_id,
                call_id=child.call_id,
                child_thread_id=child.child_thread_id,
                child_turn_id=child.child_turn_id,
                final_bytes=child.final_bytes,
                review_input=child.review_input)

    def completed_children(self) -> tuple[NativeReviewEvidence, ...]:
        """Return all currently complete children in trusted spawn order."""
        with self._lock:
            completed = []
            for child in sorted(
                    self._children_by_call.values(),
                    key=lambda item: item.spawn_order):
                if (child.final_bytes is not None and child.terminal
                        and child.read_verified):
                    completed.append(self.completed_child(child.child_thread_id))
            return tuple(completed)

    def claim_completed_child(
            self, child_thread_id: str, *, claim_id: str) -> NativeReviewEvidence:
        """Bind one proven child and one durable request, idempotently."""
        normalized_claim = _identity(claim_id, "native review claim id")
        with self._lock:
            evidence = self.completed_child(child_thread_id)
            existing = self._child_claims.get(evidence.child_thread_id)
            if existing is not None and existing != normalized_claim:
                raise NativeReviewError(
                    "native child was already claimed by another review request")
            claimed_child = self._request_claims.get(normalized_claim)
            if (claimed_child is not None
                    and claimed_child != evidence.child_thread_id):
                self._poisoned_claims.add(normalized_claim)
                raise NativeReviewError(
                    "native review request was already claimed by another "
                    "completed child")
            self._child_claims[evidence.child_thread_id] = normalized_claim
            self._request_claims[normalized_claim] = evidence.child_thread_id
            self._refresh_claim_poison(normalized_claim)
            if normalized_claim in self._poisoned_claims:
                raise NativeReviewError(
                    "native review request has multiple completed children")
            return evidence

    def verify_completed_child_claim(
            self, child_thread_id: str, *, claim_id: str) -> NativeReviewEvidence:
        """Recheck a durable request↔child binding against the live ledger."""
        child_id = _identity(child_thread_id, "child thread id")
        normalized_claim = _identity(claim_id, "native review claim id")
        with self._lock:
            evidence = self.completed_child(child_id)
            if (self._child_claims.get(child_id) != normalized_claim
                    or self._request_claims.get(normalized_claim) != child_id):
                raise NativeReviewError(
                    "native review request/child claim binding is missing")
            self._refresh_claim_poison(normalized_claim)
            if normalized_claim in self._poisoned_claims:
                raise NativeReviewError(
                    "native review request has multiple completed children")
            return evidence

    def claim_completed_child_snapshot(
            self, child_thread_id: str, *,
            claim_id: str) -> tuple[NativeReviewEvidence, bytes]:
        """Atomically claim one child and export its parsed owner JSONL prefix.

        The returned bytes come only from the runner-owned observer.  A
        trailing partial app-server line is deliberately excluded so a
        replay consumer receives a closed JSONL prefix rather than
        caller-authored evidence or an unstable stream tail.
        """
        with self._lock:
            evidence = self.claim_completed_child(
                child_thread_id, claim_id=claim_id)
            parsed_length = len(self._raw) - len(self._pending)
            if parsed_length <= 0:
                raise NativeReviewError(
                    "native review parsed owner prefix is missing")
            prefix = bytes(self._raw[:parsed_length])
            if not prefix.endswith(b"\n"):
                raise NativeReviewError(
                    "native review parsed owner prefix is not closed JSONL")
            return evidence, prefix

    def finalize(
            self, *, receipt: Mapping[str, Any],
            captured_stdout: bytes) -> tuple[NativeReviewEvidence, ...]:
        with self._lock:
            if self._finalized:
                raise NativeReviewError("ledger is already finalized")
            if self._pending:
                raise NativeReviewError("trailing partial app-server JSONL")
            if not isinstance(captured_stdout, bytes):
                raise TypeError("captured_stdout must be bytes")
            observed = bytes(self._raw)
            if captured_stdout != observed:
                raise NativeReviewError("capture bytes differ from observed stream")
            if (receipt.get("state") != "terminal"
                    or receipt.get("outcome") != "exit"):
                raise NativeReviewError("guardian receipt is not terminal exit")
            returncode = receipt.get("returncode")
            if (isinstance(returncode, bool)
                    or not isinstance(returncode, int)
                    or returncode != 0
                    or receipt.get("group_drained") is not True):
                raise NativeReviewError(
                    "guardian receipt is nonzero or not group-drained")
            receipt_bytes = receipt.get("capture_stdout_bytes")
            if (isinstance(receipt_bytes, bool)
                    or not isinstance(receipt_bytes, int)
                    or receipt_bytes != len(captured_stdout)):
                raise NativeReviewError("guardian capture stdout bytes mismatch")
            receipt_sha = receipt.get("capture_stdout_sha256")
            actual_sha = "sha256:" + hashlib.sha256(captured_stdout).hexdigest()
            if receipt_sha != actual_sha:
                raise NativeReviewError("guardian capture stdout sha256 mismatch")
            if self._parent_thread_id is None or self._parent_turn_id is None:
                raise NativeReviewError("parent identity is missing")
            if not self._parent_terminal:
                raise NativeReviewError("parent turn is not terminal")
            for claim_id in self._request_claims:
                self._refresh_claim_poison(claim_id)
            if self._poisoned_claims:
                raise NativeReviewError(
                    "native review request has multiple completed children")
            for call_id in self._spawns:
                if call_id not in self._children_by_call:
                    raise NativeReviewError("spawn did not bind a child activity")
            evidence = []
            for child in sorted(
                    self._children_by_call.values(),
                    key=lambda item: item.spawn_order):
                if child.final_bytes is None:
                    raise NativeReviewError("child final answer is missing")
                if not child.terminal:
                    raise NativeReviewError("child terminal event is missing")
                if not child.read_verified:
                    raise NativeReviewError("child thread/read proof is missing")
                assert child.child_turn_id is not None
                evidence.append(NativeReviewEvidence(
                    parent_thread_id=self._parent_thread_id,
                    parent_turn_id=self._parent_turn_id,
                    call_id=child.call_id,
                    child_thread_id=child.child_thread_id,
                    child_turn_id=child.child_turn_id,
                    final_bytes=child.final_bytes,
                    review_input=child.review_input))
            self._finalized = True
            return tuple(evidence)


def replay_native_review_live_snapshot(
        snapshot_path: Path, *, expected_snapshot_hash: str,
        expected_snapshot_bytes: int, expected_runner_call_id: int,
        expected_cycle_id: str, expected_stage: str,
        expected_purpose: str, expected_parent_thread_id: str,
        expected_parent_turn_id: str,
        expected_spawn_proof_mode: str = RAW_SPAWN_PROOF_MODE,
        ) -> NativeReviewExecutionEvidence:
    """Replay an owner-persisted, parsed JSONL prefix while its runner is live.

    This is intentionally weaker than terminal guardian replay and is valid
    only while the bound ``runner_call`` remains ``running``.  The shared
    verifier owns that lifecycle choice; this function only proves that the
    immutable prefix itself reconstructs the child spawn/input/final/terminal
    events observed by the owner-bound :class:`NativeReviewLedger`.
    """
    if (isinstance(expected_runner_call_id, bool)
            or not isinstance(expected_runner_call_id, int)
            or expected_runner_call_id <= 0):
        raise ValueError(
            "expected_runner_call_id must be a positive integer")
    if (isinstance(expected_snapshot_bytes, bool)
            or not isinstance(expected_snapshot_bytes, int)
            or expected_snapshot_bytes <= 0
            or expected_snapshot_bytes > _DEFAULT_MAX_STREAM_BYTES):
        raise ValueError("expected_snapshot_bytes is invalid")
    for label, value in (
            ("expected_cycle_id", expected_cycle_id),
            ("expected_stage", expected_stage),
            ("expected_purpose", expected_purpose),
            ("expected_parent_thread_id", expected_parent_thread_id),
            ("expected_parent_turn_id", expected_parent_turn_id)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be a non-empty string")

    from .artifact_capability import (
        ArtifactCapabilityError,
        read_artifact_bytes,
    )

    try:
        snapshot_ref = Path(snapshot_path).resolve(strict=True)
        captured = read_artifact_bytes(
            snapshot_ref, expected_hash=expected_snapshot_hash,
            expected_size=expected_snapshot_bytes,
            max_bytes=_DEFAULT_MAX_STREAM_BYTES,
            label="native review live owner snapshot")
    except (OSError, ValueError, ArtifactCapabilityError) as error:
        raise NativeReviewError(
            f"live owner snapshot cannot be read: {error}") from error
    if not captured.endswith(b"\n"):
        raise NativeReviewError(
            "live owner snapshot is not a closed JSONL prefix")

    if expected_spawn_proof_mode not in _SPAWN_PROOF_MODES:
        raise ValueError("expected_spawn_proof_mode is invalid")
    ledger = NativeReviewLedger(
        spawn_proof_mode=expected_spawn_proof_mode)
    ledger.feed(captured)
    if ledger.parent_identity() != (
            expected_parent_thread_id, expected_parent_turn_id):
        raise NativeReviewError(
            "live owner snapshot parent identity mismatch")
    children = ledger.completed_children()
    if not children:
        raise NativeReviewError(
            "live owner snapshot has no completed reviewer child")
    return NativeReviewExecutionEvidence(
        runner_call_id=expected_runner_call_id,
        cycle_id=expected_cycle_id,
        stage=expected_stage,
        purpose=expected_purpose,
        execution_receipt_ref=str(snapshot_ref),
        execution_operation_id=(
            "live-prefix:" + expected_snapshot_hash.removeprefix("sha256:")),
        capture_stdout_sha256=expected_snapshot_hash,
        children=children,
        spawn_proof_mode=expected_spawn_proof_mode)


def replay_native_review_execution(
        execution_receipt_path: Path, *, expected_runner_call_id: int,
        expected_cycle_id: str, expected_stage: str,
        expected_purpose: str) -> NativeReviewExecutionEvidence:
    """Rebuild live-ledger evidence from the guardian's durable capture.

    The caller supplies identities derived from ``runner_call`` rather than
    from a model-authored review receipt.  ``process_supervisor`` then verifies
    the no-follow receipt/capture identity before this module replays the exact
    app-server event stream through the same ledger used online.
    """
    if (isinstance(expected_runner_call_id, bool)
            or not isinstance(expected_runner_call_id, int)
            or expected_runner_call_id <= 0):
        raise ValueError("expected_runner_call_id must be a positive integer")
    for label, value in (
            ("expected_cycle_id", expected_cycle_id),
            ("expected_stage", expected_stage),
            ("expected_purpose", expected_purpose)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be a non-empty string")

    # Local import avoids making the guardian depend on the review parser.
    from .process_supervisor import (
        read_execution_capture,
        read_receipt,
        validate_execution_receipt,
    )

    receipt_path = Path(execution_receipt_path).resolve(strict=True)
    receipt = read_receipt(receipt_path)
    validate_execution_receipt(receipt, receipt_path)
    context = receipt.get("context")
    spawn_proof_mode = (
        context.get(
            "native_review_spawn_proof_mode",
            RAW_SPAWN_PROOF_MODE)
        if isinstance(context, dict) else None)
    if (spawn_proof_mode not in _SPAWN_PROOF_MODES
            or receipt.get("kind") not in {
                "codex-resident-stage", "codex-stage-main"}
            or receipt.get("state") != "terminal"
            or receipt.get("outcome") != "exit"
            or receipt.get("returncode") != 0
            or receipt.get("group_drained") is not True
            or not isinstance(context, dict)
            or context.get("reconcile_protocol") != "runner-call-v1"
            or context.get("db_owner_kind") != "runner_call"
            or context.get("db_owner_id") != expected_runner_call_id
            or context.get("cycle_id") != expected_cycle_id
            or context.get("stage") != expected_stage
            or context.get("db_phase") != expected_stage
            or context.get("db_purpose") != expected_purpose):
        raise ValueError(
            "native review execution receipt does not match runner_call scope")
    operation_id = receipt.get("operation_id")
    capture_sha256 = receipt.get("capture_stdout_sha256")
    if (not isinstance(operation_id, str)
            or not isinstance(capture_sha256, str)):
        raise ValueError("native review execution receipt identity is missing")
    captured = read_execution_capture(receipt, stream="stdout")
    ledger = NativeReviewLedger(
        spawn_proof_mode=spawn_proof_mode)
    ledger.feed(captured)
    children = ledger.finalize(
        receipt=receipt, captured_stdout=captured)
    return NativeReviewExecutionEvidence(
        runner_call_id=expected_runner_call_id,
        cycle_id=expected_cycle_id,
        stage=expected_stage,
        purpose=expected_purpose,
        execution_receipt_ref=str(receipt_path),
        execution_operation_id=operation_id,
        capture_stdout_sha256=capture_sha256,
        children=children,
        spawn_proof_mode=spawn_proof_mode)
