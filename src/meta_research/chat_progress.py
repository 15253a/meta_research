"""Bounded, read-only previews of the current provider operation's answer.

Previews are not domain results. Only the normal final response validation may
commit an assistant reply. Tool output and reasoning events never enter previews.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CHAT_PROGRESS_READ_MAX_BYTES = 512 * 1024
CHAT_PROGRESS_CACHE_MAX_FILES = 64
_CHAT_PROGRESS_ANCHOR_BYTES = 128
CHAT_REPLY_MAX_LENGTH = 12000
CHAT_REPLY_PROGRESS_INSTRUCTION = (
    "\n\n在形成回答时，请用 commentary 逐段给出面向用户的答复正文，"
    "每形成一段即可输出；只输出适合用户直接阅读的内容，不输出内部推理、"
    "工具命令、工具结果或原始 JSON。最后仍按指定 schema 返回结果，"
    "其中 reply 包含完整答复正文。"
)


def preserve_existing_reply_prompt(
    prompt: str,
    *,
    invocation_path: Path,
    job_ref: str,
    hash_prompt: Callable[[str], str],
    operation_name: str | None = None,
) -> str:
    """Select the pre-progress prompt only when this job already bound its hash.

    This does not accept or modify a durable operation: the normal invocation
    path still verifies its signature, full identity and result before reuse.
    An unmatched or unreadable record keeps the current prompt, so conflicts
    retain the existing fail-closed recovery behavior.
    """
    try:
        with invocation_path.open("rb") as stream:
            encoded = stream.read(64 * 1024 + 1)
        if len(encoded) > 64 * 1024:
            return prompt
        invocation = json.loads(encoded)
    except (OSError, ValueError, RecursionError):
        return prompt
    if operation_name is not None:
        invocation = invocation.get("payload") if isinstance(invocation, dict) else None
    if (
        not isinstance(invocation, dict)
        or invocation.get("job_ref") != job_ref
        or (operation_name is not None and invocation.get("operation_name") != operation_name)
    ):
        return prompt
    legacy_prompt = prompt.replace(CHAT_REPLY_PROGRESS_INSTRUCTION, "", 1)
    if invocation.get("prompt_hash") == hash_prompt(legacy_prompt):
        return legacy_prompt
    return prompt


def _string_prefix(source: str, start: int) -> str:
    """Decode only complete JSON string characters, including surrogate pairs."""
    if start >= len(source) or source[start] != '"':
        return ""
    output: list[str] = []
    cursor = start + 1
    escapes = {
        '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
        "n": "\n", "r": "\r", "t": "\t",
    }
    while cursor < len(source) and len(output) < CHAT_REPLY_MAX_LENGTH:
        char = source[cursor]
        if char == '"':
            break
        if char != "\\":
            if ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF:
                break
            output.append(char)
            cursor += 1
            continue
        if cursor + 1 >= len(source):
            break
        escaped = source[cursor + 1]
        if escaped in escapes:
            output.append(escapes[escaped])
            cursor += 2
            continue
        if escaped != "u" or cursor + 6 > len(source):
            break
        try:
            point = int(source[cursor + 2:cursor + 6], 16)
        except ValueError:
            break
        cursor += 6
        if 0xD800 <= point <= 0xDBFF:
            if source[cursor:cursor + 2] != "\\u" or cursor + 6 > len(source):
                break
            try:
                low = int(source[cursor + 2:cursor + 6], 16)
            except ValueError:
                break
            if not 0xDC00 <= low <= 0xDFFF:
                break
            point = 0x10000 + ((point - 0xD800) << 10) + low - 0xDC00
            cursor += 6
        elif 0xDC00 <= point <= 0xDFFF:
            break
        output.append(chr(point))
    return "".join(output)


def _answer_text(text: str) -> tuple[bool, str]:
    source = text.lstrip()
    if source.startswith("```"):
        opening, separator, body = source.partition("\n")
        if opening.strip().lower() == "```json":
            source = body.lstrip() if separator else "{"
        elif opening.strip() == "```" and body.lstrip().startswith(("{", "[")):
            source = body.lstrip()
    if not source.startswith(("{", "[")):
        return False, text[:CHAT_REPLY_MAX_LENGTH]
    if not source.startswith("{"):
        return True, ""
    decoder = json.JSONDecoder()
    cursor = 1
    # Walk only top-level properties; a nested/tool object's reply is not ours.
    while cursor < len(source):
        cursor += len(source[cursor:]) - len(source[cursor:].lstrip())
        try:
            key, cursor = decoder.raw_decode(source, cursor)
        except (ValueError, RecursionError):
            break
        if not isinstance(key, str):
            break
        cursor += len(source[cursor:]) - len(source[cursor:].lstrip())
        if source[cursor:cursor + 1] != ":":
            break
        cursor += 1
        cursor += len(source[cursor:]) - len(source[cursor:].lstrip())
        if key == "reply":
            return True, _string_prefix(source, cursor)
        try:
            _value, cursor = decoder.raw_decode(source, cursor)
        except (ValueError, RecursionError):
            break
        cursor += len(source[cursor:]) - len(source[cursor:].lstrip())
        if source[cursor:cursor + 1] != ",":
            break
        cursor += 1
    return True, ""


@dataclass
class _ReplyProgress:
    identity: tuple[int, int]
    offset: int = 0
    observed_size: int = 0
    modified_ns: int = 0
    anchor: bytes = b""
    partial_record: bytes = b""
    discard_record: bool = False
    paragraphs: dict[bytes, str] = field(default_factory=dict)
    reply: str = ""

    def text(self) -> str:
        return "\n\n".join(self.paragraphs.values())

    def accept(self, record: bytes) -> None:
        try:
            event = json.loads(record)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "turn.completed":
            # CLI JSON events omit message phase. The last schema result is
            # authoritative only once the turn ends; it may revise previews.
            if self.reply:
                self.paragraphs = {b"terminal-reply": self.reply}
            return
        if event.get("type") not in {
            "item.started", "item.updated", "item.completed"
        }:
            return
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            return
        public_channels = {None, "commentary", "final", "final_answer"}
        if (
            item.get("phase") not in public_channels
            or item.get("channel") not in public_channels
            or item.get("role") not in {None, "assistant"}
        ):
            return
        text = item.get("text")
        if not isinstance(text, str):
            return
        structured, answer = _answer_text(text)
        if structured:
            if not answer:
                return
            if not self.reply:
                self.paragraphs.clear()
        elif self.reply or not answer.strip():
            return
        identifier = item.get("id")
        # Hash identities so provider-controlled IDs cannot grow cache memory.
        identity = identifier if isinstance(identifier, str) else text
        identity_hash = hashlib.sha256(identity.encode("utf-8")).digest()
        previous = self.paragraphs.get(identity_hash)
        if previous == answer:
            return
        if structured:
            self.reply = answer
            current = self.text()
            if item.get("phase") in {"final", "final_answer"} or item.get("channel") in {"final", "final_answer"}:
                self.paragraphs.clear()
                previous = None
            elif previous is None and current:
                # The CLI also schema-wraps each commentary paragraph. Keep
                # distinct items, while a cumulative final replaces its prefix.
                # Partial cumulative updates must not repeat or erase text.
                if current.startswith(answer):
                    return
                if answer.startswith(current):
                    self.paragraphs.clear()
        used = sum(len(value) for value in self.paragraphs.values())
        used += max(0, len(self.paragraphs) - 1) * 2
        if previous is not None:
            available = CHAT_REPLY_MAX_LENGTH - used + len(previous)
        else:
            available = CHAT_REPLY_MAX_LENGTH - used - (2 if self.paragraphs else 0)
        if available > 0:
            self.paragraphs[identity_hash] = answer[:available]

    def consume(self, content: bytes) -> None:
        if self.discard_record:
            _skipped, separator, content = content.partition(b"\n")
            if not separator:
                return
            self.discard_record = False
        records = (self.partial_record + content).split(b"\n")
        for record in records[:-1]:
            if len(record) <= CHAT_PROGRESS_READ_MAX_BYTES:
                self.accept(record)
        self.partial_record = records[-1]
        if len(self.partial_record) > CHAT_PROGRESS_READ_MAX_BYTES:
            self.partial_record = b""
            self.discard_record = True


_reply_progress: OrderedDict[str, _ReplyProgress] = OrderedDict()
_reply_progress_lock = threading.Lock()


def read_chat_reply(stdout_path: Path) -> str:
    """Read this operation incrementally, preserving already streamed paragraphs.

    At most 64 files are cached. Each observation reads at most 512 KiB, including
    the small append-verification anchor. New/evicted files are scanned from the
    beginning over subsequent observations, so large tool gaps never drop text.
    Incomplete records stay bounded; oversized records are skipped to a newline.
    File replacement, truncation or a changed anchor resets only that file.
    """
    cache_key = os.path.normcase(str(stdout_path.absolute()))
    with _reply_progress_lock:
        try:
            if stdout_path.is_symlink():
                _reply_progress.pop(cache_key, None)
                return ""
            with stdout_path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                state = _reply_progress.get(cache_key)
                if (
                    state is None
                    or state.identity != identity
                    or stat.st_size < state.offset
                    or (
                        stat.st_mtime_ns != state.modified_ns
                        and stat.st_size <= state.observed_size
                    )
                ):
                    state = _ReplyProgress(identity)
                read_budget = CHAT_PROGRESS_READ_MAX_BYTES
                if state.anchor and (
                    stat.st_mtime_ns != state.modified_ns
                    or stat.st_size != state.observed_size
                ):
                    stream.seek(state.offset - len(state.anchor))
                    anchor = stream.read(len(state.anchor))
                    read_budget -= len(state.anchor)
                    if anchor != state.anchor:
                        state = _ReplyProgress(identity)
                if stat.st_size > state.offset:
                    stream.seek(state.offset)
                    content = stream.read(read_budget)
                    state.offset += len(content)
                    state.anchor = (state.anchor + content)[-_CHAT_PROGRESS_ANCHOR_BYTES:]
                    state.consume(content)
                state.observed_size = stat.st_size
                state.modified_ns = stat.st_mtime_ns
        except OSError:
            _reply_progress.pop(cache_key, None)
            return ""
        _reply_progress[cache_key] = state
        _reply_progress.move_to_end(cache_key)
        while len(_reply_progress) > CHAT_PROGRESS_CACHE_MAX_FILES:
            _reply_progress.popitem(last=False)
        return state.text()
