"""Read-only child lifecycle metadata from an already bound provider spool.

The caller establishes Owner/operation scope. This reader never launches work,
uses review reservations as activity, or publishes child text or prompts.
"""
from __future__ import annotations

import json
from pathlib import Path

from meta_research.harness_adapters import _codex_item_actor, _claude_content_blocks
from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA, SUPERVISOR_EXIT_SCHEMA_V2,
    read_transport_envelope, read_transport_key_for_operation, transport_file_sha256,
)

_TERMINAL = {"completed", "failed", "cancelled"}
_MAX_BYTES = 64 * 1024 * 1024
_MAX_LINE = 1024 * 1024
_MAX_CHILDREN = 256


def _ref(value):
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


def _status(value):
    if value in {"completed", "succeeded"}:
        return "completed"
    if value in {"errored", "failed"}:
        return "failed"
    if value in {"cancelled", "canceled", "interrupted", "shutdown", "stopped", "killed"}:
        return "cancelled"
    if value in {"running", "in_progress"}:
        return "running"
    if value in {"pending", "starting"}:
        return "starting"
    return "unknown"


def read_child_sessions(directory: Path, *, invocation_hash: str,
                        parent_session_ref: str, operation_ref: str,
                        executing: bool, expected_native: str | None = None,
                        known_children=(), provider="codex"):
    """Return bounded safe metadata; a vanished active process means unknown."""
    source = directory / "stdout.jsonl"
    if not source.exists():
        return [], False
    if source.is_symlink() or not source.is_file() or source.stat().st_size > _MAX_BYTES:
        raise ValueError("child_session_source_unavailable")
    receipt_path = directory / "supervisor-exit.json"
    if receipt_path.exists():
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise ValueError("child_session_source_unavailable")
        _, key = read_transport_key_for_operation(directory)
        receipt = read_transport_envelope(receipt_path, key)
        if (receipt.get("schema_ref") not in {SUPERVISOR_EXIT_SCHEMA, SUPERVISOR_EXIT_SCHEMA_V2}
                or receipt.get("invocation_hash") != invocation_hash
                or receipt.get("stdout_file_hash") != transport_file_sha256(source)):
            raise ValueError("child_session_source_unavailable")
        executing = False
    native, children, requests, limited, read_bytes = expected_native, {}, {}, False, 0
    known_loaded = False
    updated_at = source.stat().st_mtime
    with source.open("rb") as stream:
        while raw := stream.readline(_MAX_LINE + 1):
            read_bytes += len(raw)
            if len(raw) > _MAX_LINE or read_bytes > _MAX_BYTES or not raw.endswith(b"\n"):
                limited = True
                break
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                limited = True
                break
            if not isinstance(event, dict):
                limited = True
                break
            root_start = (provider == "codex" and event.get("type") == "thread.started"
                          and not event.get("parent_thread_id")) or (
                          provider == "claude" and event.get("type") == "system"
                          and event.get("subtype") == "init" and not event.get("parent_tool_use_id"))
            if root_start:
                observed = _ref(event.get("thread_id" if provider == "codex" else "session_id"))
                if observed is None or native is not None and observed != native:
                    raise ValueError("child_session_native_identity_invalid")
                native = observed
            if native is None:
                continue
            if not known_loaded:
                for child in known_children:
                    if (child["parent_session_ref"] == parent_session_ref and child["provider"] == provider
                            and child["parent_native_session_ref"] == native
                            and child["native_session_ref"] is not None):
                        children[child["native_session_ref"]] = dict(child)
                known_loaded = True
            if provider == "claude":
                if event.get("session_id") != native or event.get("parent_tool_use_id"):
                    continue
                if event.get("type") == "assistant":
                    for block in _claude_content_blocks(event):
                        tool = _ref(block.get("id"))
                        if (block.get("type") == "tool_use" and block.get("name") in {"Agent", "Task", "Subagent"}
                                and tool is not None and tool not in requests):
                            if len(children) >= _MAX_CHILDREN:
                                limited = True
                                continue
                            row = _child(parent_session_ref, operation_ref, native, None, provider, updated_at,
                                         request=tool)
                            row["status"] = "starting"
                            children["request:" + tool] = requests[tool] = row
                if event.get("type") == "system":
                    task, tool = _ref(event.get("task_id")), _ref(event.get("tool_use_id"))
                    subtype = event.get("subtype")
                    row = children.get(task)
                    if (subtype == "task_started" and task is not None and tool in requests
                            and event.get("task_type") == "local_agent"
                            and event.get("spawn_depth", 1) == 1):
                        row = children.get(task) or requests[tool]
                        if row["native_session_ref"] not in (None, task):
                            raise ValueError("child_session_native_identity_invalid")
                        row["native_session_ref"] = task
                        children.pop("request:" + tool, None)
                        children[task] = requests[tool] = row
                        row["status"] = "running"
                    elif row is not None and subtype == "task_progress" and row["status"] not in _TERMINAL:
                        row["status"] = "running"
                    elif row is not None and subtype in {"task_notification", "task_updated"}:
                        patch = event.get("patch")
                        status = _status(event.get("status") if subtype == "task_notification" else
                                         patch.get("status") if isinstance(patch, dict) else None)
                        if status in _TERMINAL and row["status"] not in _TERMINAL:
                            row["status"] = status
                    if row is not None and subtype in {"task_started", "task_progress", "task_notification", "task_updated"}:
                        row["operation_ref"], row["updated_at"] = operation_ref, updated_at
                if event.get("type") == "result":
                    executing = False
                continue
            item = event.get("item")
            if (isinstance(item, dict) and item.get("type") == "collab_tool_call"
                    and _codex_item_actor(event, item) == native
                    and not event.get("parent_thread_id") and not item.get("parent_thread_id")
                    and event.get("type") == "item.completed"
                    and item.get("status") == "completed"):
                receivers = item.get("receiver_thread_ids")
                states = item.get("agents_states")
                if not isinstance(receivers, list) or not isinstance(states, dict):
                    continue
                limited = limited or len(receivers) > _MAX_CHILDREN
                for child in receivers[:_MAX_CHILDREN]:
                    child = _ref(child)
                    if child is None or child == native:
                        continue
                    if item.get("tool") == "spawn_agent" and child not in children:
                        if len(children) >= _MAX_CHILDREN:
                            limited = True
                            continue
                        children[child] = _child(parent_session_ref, operation_ref,
                                                 native, child, "codex", updated_at)
                    row = children.get(child)
                    state = states.get(child)
                    if (row is not None and isinstance(state, dict)
                            and item.get("tool") in {"spawn_agent", "wait", "close_agent", "send_input", "resume_agent"}):
                        status = _status(state.get("status"))
                        if row["status"] not in _TERMINAL or item.get("tool") in {"send_input", "resume_agent"}:
                            row["status"] = status
                            row["operation_ref"] = operation_ref
                            row["updated_at"] = updated_at
            if (event.get("type") in {"turn.completed", "turn.failed"}
                    and event.get("thread_id") in (None, native) and not event.get("parent_thread_id")):
                executing = False
    for child in children.values():
        if limited or child["status"] not in _TERMINAL and not executing:
            child["status"] = "unknown"
    return list(children.values()), limited


def _child(parent, operation, native, child, provider, updated_at, *, request=None):
    return {"session_ref": "child-session:" + canonical_hash({
                "parent": parent, "provider": provider,
                **({"native": child} if request is None else {"operation": operation, "request": request})}),
            "parent_session_ref": parent, "parent_native_session_ref": native,
            "native_session_ref": child, "operation_ref": operation,
            "provider": provider, "status": "unknown", "updated_at": updated_at,
            "label": "子智能体"}


def append_child_sessions(session, children):
    """Preserve one identity when a root continues in another provider turn."""
    existing = {child["session_ref"]: child for child in session.setdefault("children", [])}
    for child in children:
        existing[child["session_ref"]] = child
    session["children"] = list(existing.values())
