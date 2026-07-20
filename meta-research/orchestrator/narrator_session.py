"""Quest-private durable identity for the read-only narrator Codex thread.

The Codex rollout itself lives in the query worker's ``CODEX_HOME``.  This
small root-owned sidecar stores only the provider thread/session id and the
durable runner-call handoff needed to resume it after an owner restart.  It is
private state: the Web API exposes only :func:`public_narrator_session_status`,
which replaces the provider id with a one-way fingerprint.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .process_supervisor import atomic_write_receipt, read_receipt


PROTOCOL = "meta-research-narrator-session-v1"
VERSION = 1
STATE_RELATIVE_PATH = Path("state") / "narrator-session.json"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CYCLE_RE = re.compile(r"^c(?:0|[1-9][0-9]*)$")
_PHASE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_PURPOSE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_TOP_KEYS = frozenset({
    "protocol", "version", "prompt_version", "session_id",
    "session_id_kind", "turn_count", "last_runner_call_id", "pending",
})
_PENDING_KEYS = frozenset({"runner_call_id", "cycle_id", "phase", "purpose"})


class NarratorSessionError(RuntimeError):
    """The narrator continuity sidecar is missing authority or is malformed."""


def new_narrator_session_state(prompt_version: str) -> Dict[str, Any]:
    if not isinstance(prompt_version, str) or _HEX64_RE.fullmatch(prompt_version) is None:
        raise NarratorSessionError("narrator prompt_version 非法")
    return {
        "protocol": PROTOCOL,
        "version": VERSION,
        "prompt_version": prompt_version,
        "session_id": None,
        "session_id_kind": None,
        "turn_count": 0,
        "last_runner_call_id": None,
        "pending": None,
    }


def validate_narrator_session_state(value: object) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _TOP_KEYS:
        raise NarratorSessionError("narrator session 顶层字段非法")
    if value.get("protocol") != PROTOCOL or value.get("version") != VERSION:
        raise NarratorSessionError("narrator session protocol/version 非法")
    prompt_version = value.get("prompt_version")
    if not isinstance(prompt_version, str) or _HEX64_RE.fullmatch(prompt_version) is None:
        raise NarratorSessionError("narrator session prompt_version 非法")
    session_id = value.get("session_id")
    session_kind = value.get("session_id_kind")
    if session_id is None:
        if session_kind is not None:
            raise NarratorSessionError("narrator session id 缺失时 kind 也须为空")
    elif (not isinstance(session_id, str)
          or _SESSION_ID_RE.fullmatch(session_id) is None
          or session_kind not in {"thread_id", "session_id"}):
        raise NarratorSessionError("narrator session id/kind 非法")
    turn_count = value.get("turn_count")
    if (isinstance(turn_count, bool) or not isinstance(turn_count, int)
            or not 0 <= turn_count < (1 << 63)):
        raise NarratorSessionError("narrator session turn_count 非法")
    last_call = value.get("last_runner_call_id")
    if last_call is not None and (
            isinstance(last_call, bool) or not isinstance(last_call, int) or last_call <= 0):
        raise NarratorSessionError("narrator session last_runner_call_id 非法")
    pending = value.get("pending")
    if pending is not None:
        if not isinstance(pending, Mapping) or set(pending) != _PENDING_KEYS:
            raise NarratorSessionError("narrator session pending 字段非法")
        runner_call_id = pending.get("runner_call_id")
        if (isinstance(runner_call_id, bool) or not isinstance(runner_call_id, int)
                or runner_call_id <= 0):
            raise NarratorSessionError("narrator session pending runner_call_id 非法")
        if (not isinstance(pending.get("cycle_id"), str)
                or _CYCLE_RE.fullmatch(pending["cycle_id"]) is None
                or not isinstance(pending.get("phase"), str)
                or _PHASE_RE.fullmatch(pending["phase"]) is None
                or not isinstance(pending.get("purpose"), str)
                or _PURPOSE_RE.fullmatch(pending["purpose"]) is None):
            raise NarratorSessionError("narrator session pending identity 非法")
    return dict(value)


def load_narrator_session_state(path: Path) -> Dict[str, Any]:
    try:
        value = read_receipt(Path(path))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as error:
        raise NarratorSessionError("narrator session sidecar 不可安全读取") from error
    return validate_narrator_session_state(value)


def write_narrator_session_state(path: Path, value: Mapping[str, Any]) -> None:
    state = validate_narrator_session_state(value)
    try:
        atomic_write_receipt(Path(path), state)
    except (OSError, ValueError) as error:
        raise NarratorSessionError("narrator session sidecar 无法耐久写入") from error


def public_narrator_session_status(work_root: Path) -> Dict[str, Any]:
    """Return a path- and provider-id-free Web projection."""
    path = Path(work_root) / STATE_RELATIVE_PATH
    try:
        state = load_narrator_session_state(path)
    except FileNotFoundError:
        return {
            "mode": "quest_persistent_resume",
            "state": "awaiting_first_turn",
            "persistent": True,
            "session_ref": None,
            "turn_count": 0,
        }
    except NarratorSessionError:
        return {
            "mode": "quest_persistent_resume",
            "state": "corrupt",
            "persistent": True,
            "session_ref": None,
            "turn_count": None,
        }
    session_id: Optional[str] = state["session_id"]
    if session_id is not None:
        status = "resuming" if state["pending"] is not None else "ready"
        session_ref = "codex:" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:10]
    else:
        status = "establishing" if state["pending"] is not None else "awaiting_first_turn"
        session_ref = None
    return {
        "mode": "quest_persistent_resume",
        "state": status,
        "persistent": True,
        "session_ref": session_ref,
        "turn_count": state["turn_count"],
    }


__all__ = [
    "NarratorSessionError",
    "STATE_RELATIVE_PATH",
    "load_narrator_session_state",
    "new_narrator_session_state",
    "public_narrator_session_status",
    "validate_narrator_session_state",
    "write_narrator_session_state",
]
