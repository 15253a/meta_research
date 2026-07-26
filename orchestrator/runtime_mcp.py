"""Quest-scoped MCP persistence tools for a live Codex stage worker.

The design is deliberately small:

* Codex talks MCP over stdio to this file's bridge mode.
* The bridge forwards one authenticated request over a local Unix socket.
* The owner process keeps the only SQLite writer and handles the request through
  :class:`RuntimeMCPBroker` and the existing :class:`WriteDaemon`.

This gives the live Codex turn immediate validation/database feedback without
granting the model a SQLite path or moving research decisions through another
model call.  Besides small indexes (reviews, baseline identities, cards and
cycle summaries), the resident main worker submits its complete stage artifact
through this boundary.  Payloads live in the file manager; SQLite stores only a
path/hash receipt.  Question creation/closure, phase transitions and promotion
of a baseline to ``legal`` retain their core consistency checks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import socket
import socketserver
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, ContextManager, Dict, Mapping, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orchestrator.native_review import (
        NativeReviewError,
        NativeReviewEvidence,
        NativeReviewExecutionEvidence,
        NativeReviewLedger,
    )
else:
    from .native_review import (
        NativeReviewError,
        NativeReviewEvidence,
        NativeReviewExecutionEvidence,
        NativeReviewLedger,
    )

_MAX_MESSAGE_BYTES = 1024 * 1024
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_NATIVE_REVIEW_ROUNDS = 32
_PROTOCOL_VERSION = "2025-06-18"
_BUNDLE_SCHEDULER_TOOLS = frozenset({
    "bundle_overview", "bundle_dispatch", "bundle_wait", "bundle_drain",
})
_BUNDLE_WORKER_TOOLS = frozenset({
    "bundle_execute", "bundle_status", "bundle_repair", "bundle_replan",
    "submit_stage_artifact", "prepare_review", "read_review_input",
    "record_review",
})


def _tool_definitions() -> list[Dict[str, Any]]:
    """Return the exact MCP tool inventory shared by bridge and owner."""
    return [
        {
            "name": "get_runtime_index",
            "description": (
                "Read the current cycle's compact question/baseline/card index. "
                "Use this before creating a new baseline identity or card."
            ),
            "inputSchema": {
                "type": "object", "additionalProperties": False,
                "properties": {},
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "preflight_plan",
            "description": (
                "Read-only Plan preflight for the complete proposed plan. It "
                "checks schema, fixed resource policy, baseline identity "
                "conflicts and returns the current compact index. It never "
                "claims a baseline; the Plan core gate performs the only claim."
            ),
            "inputSchema": {
                "type": "object", "required": ["plan"],
                "additionalProperties": False,
                "properties": {"plan": {"type": "object"}},
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "plan_import_search",
            "description": (
                "Plan-only trusted repository/literature discovery inside the "
                "current resident Plan turn. Pass the exact import-search request "
                "allowed by the frozen Plan anchor. The owner executes the bounded, "
                "replayable connector and returns a small result plus a managed "
                "refreshed ContextPack index; read that index and continue authoring "
                "the final plan in this same top-level Codex thread."
            ),
            "inputSchema": {
                "type": "object", "required": ["request"],
                "additionalProperties": False,
                "properties": {"request": {"type": "object"}},
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "wildidea_expand",
            "description": (
                "Idea-only WildIdea generation capability. Use inside the "
                "resident Idea main turn; for generation_path=wildidea it runs "
                "the pinned internal generator and returns its exact owner-bound "
                "draft without replacing the resident top-level main session."
            ),
            "inputSchema": {
                "type": "object", "required": ["need_innovation"],
                "additionalProperties": False,
                "properties": {"need_innovation": {"type": "boolean"}},
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "wildidea_search",
            "description": (
                "Idea-only controlled novelty search for candidate queries. "
                "The configured provider returns replayable result/snapshot "
                "identities; when disabled the tool reports that explicitly."
            ),
            "inputSchema": {
                "type": "object", "required": ["queries"],
                "additionalProperties": False,
                "properties": {
                    "queries": {
                        "type": "array", "minItems": 1, "maxItems": 12,
                        "items": {"type": "string", "minLength": 5, "maxLength": 512},
                    },
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "wildidea_audit",
            "description": (
                "Idea-only completion of a server-bound generation_path=wildidea "
                "route. Pass the WildIdea draft produced from wildidea_expand. "
                "The capability runs the pinned blind audit and deterministic "
                "merge internally, then returns the exact idea_set that the "
                "resident Idea main must absorb and submit unchanged."
            ),
            "inputSchema": {
                "type": "object", "required": ["draft"],
                "additionalProperties": False,
                "properties": {"draft": {"type": "object"}},
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": False},
        },
        {
            "name": "bundle_overview",
            "description": (
                "Scheduler-only compact DAG projection. Returns ready, active, "
                "resource-waiting and terminal targets plus a monotonic revision. "
                "It never returns source trees or raw experiment logs."
            ),
            "inputSchema": {
                "type": "object", "additionalProperties": False,
                "properties": {},
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "bundle_dispatch",
            "description": (
                "Scheduler-only dispatch of the currently ready frontier. "
                "Trusted orchestration rechecks dependency admission, worker "
                "slots, source bindings and resource leases before starting "
                "one fixed-target Worker task per admitted assignment."
            ),
            "inputSchema": {
                "type": "object", "additionalProperties": False,
                "properties": {},
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "bundle_wait",
            "description": (
                "Scheduler-only bounded wait for a DAG revision newer than "
                "after_revision, a terminal/failure transition, or timeout. "
                "The result is compact and contains no raw experiment logs."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["after_revision", "timeout_s"],
                "additionalProperties": False,
                "properties": {
                    "after_revision": {"type": "integer", "minimum": 0},
                    "timeout_s": {
                        "type": "number", "minimum": 0, "maximum": 1800
                    },
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "bundle_drain",
            "description": (
                "Scheduler-only dispatch fence and bounded drain request. "
                "No new Worker is started after the fence; active guardians "
                "and resource leases must settle before Bundle can close."
            ),
            "inputSchema": {
                "type": "object", "additionalProperties": False,
                "properties": {},
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "bundle_next_target",
            "description": (
                "Legacy cycle-wide serial Bundle binding. New production "
                "Scheduler/Target-Worker sessions do not receive this "
                "capability; it remains temporarily visible for replaying "
                "pre-DAG in-flight sessions."
            ),
            "inputSchema": {
                "type": "object", "additionalProperties": False,
                "properties": {},
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "bundle_execute",
            "description": (
                "Target-Worker-only asynchronous start or admission-resume for "
                "the one build_target fixed in this capability. Pass the exact "
                "receipt returned by submit_stage_artifact. The Worker cannot "
                "change target, argv, image, GPU lease, environment or inputs."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["submission_ref", "submission_hash"],
                "additionalProperties": False,
                "properties": {
                    "submission_ref": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "submission_hash": {
                        "type": "string", "pattern": "^sha256:[0-9a-f]{64}$"
                    },
                },
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": False},
        },
        {
            "name": "bundle_status",
            "description": (
                "Target-Worker-only bounded status read for its fixed target. "
                "Use mode=snapshot once on entry/recovery, then mode=incremental "
                "with the returned cursor. Default limit is 200 and the hard "
                "maximum is 1000; responses never contain unbounded log history."
            ),
            "inputSchema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "mode": {"enum": ["snapshot", "incremental"]},
                    "after_seq": {"type": "integer", "minimum": 0},
                    "after_status_revision": {
                        "type": "integer", "minimum": 0,
                    },
                    "limit": {
                        "type": "integer", "minimum": 1, "maximum": 1000,
                    },
                    "timeout_s": {
                        "type": "number", "minimum": 0, "maximum": 1800,
                    },
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "bundle_repair",
            "description": (
                "Request cancellation of the currently running official "
                "command after bundle_status exposes a concrete engineering "
                "failure. The guardian drains the process tree; poll status, "
                "then submit the repaired complete bundle in this same turn."
            ),
            "inputSchema": {
                "type": "object", "required": ["diagnosis_md"],
                "additionalProperties": False,
                "properties": {
                    "diagnosis_md": {"type": "string", "minLength": 1, "maxLength": 8192},
                },
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "bundle_replan",
            "description": (
                "Bundle-only explicit frozen-plan escalation. Use only when the "
                "official execution feedback proves the plan/protocol itself is "
                "unexecutable and code/environment repair cannot solve it."
            ),
            "inputSchema": {
                "type": "object", "required": ["diagnosis_md"],
                "additionalProperties": False,
                "properties": {
                    "diagnosis_md": {"type": "string", "minLength": 1, "maxLength": 8192},
                },
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "upsert_card",
            "description": (
                "Create or refresh a compact index card for an existing question, "
                "baseline, method/protocol, or Bundle failure. Method cards are "
                "stored in the existing protocol-card namespace."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["card_type", "ref_id", "card_md"],
                "additionalProperties": False,
                "properties": {
                    "card_type": {"enum": ["question", "baseline", "method", "failure"]},
                    "ref_id": {"type": "integer", "minimum": 1},
                    "card_md": {"type": "string", "minLength": 1, "maxLength": 262144},
                },
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "prepare_review",
            "description": (
                "Materialize the exact candidate for the server-selected next "
                "native child-review round. Spawn one child with fork_turns=none "
                "and only the returned review_request_id; that child must call "
                "read_review_input before returning its findings. Then revise "
                "in the resident main context and call record_review."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["review_kind"],
                "additionalProperties": False,
                "properties": {
                    "review_kind": {
                        "enum": ["idea", "plan", "bundle_code", "bundle_result"]
                    },
                    "files": {"type": "object", "maxProperties": 1024},
                    "workspace_files": {
                        "type": "array", "maxItems": 1024,
                        "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    },
                    "md": {"type": "string", "maxLength": 262144},
                },
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": False},
        },
        {
            "name": "read_review_input",
            "description": (
                "Reviewer-child-only delivery of the owner-verified canonical "
                "review brief. Call once with the prepared review_request_id "
                "before writing the terminal native-review-result-v1."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["review_request_id"],
                "additionalProperties": False,
                "properties": {
                    "review_request_id": {
                        "type": "string", "minLength": 1, "maxLength": 256,
                    },
                },
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "record_review",
            "description": (
                "Complete one prepared native child-review round. Reviewer "
                "findings are read only from the trusted live child ledger; "
                "provide a disposition for every finding and the revised candidate."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["review_request_id", "dispositions"],
                "additionalProperties": False,
                "properties": {
                    "review_request_id": {
                        "type": "string", "minLength": 1, "maxLength": 256,
                    },
                    "dispositions": {
                        "type": "array", "maxItems": 64,
                        "items": {
                            "type": "object",
                            "required": ["finding_id", "decision", "rationale"],
                            "additionalProperties": False,
                            "properties": {
                                "finding_id": {"type": "string", "minLength": 1},
                                "decision": {"enum": ["accept", "reject"]},
                                "rationale": {
                                    "type": "string", "minLength": 1,
                                    "maxLength": 4096,
                                },
                            },
                        },
                    },
                    "files": {"type": "object", "maxProperties": 1024},
                    "workspace_files": {
                        "type": "array", "maxItems": 1024,
                        "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    },
                    "md": {"type": "string", "maxLength": 262144},
                },
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": False},
        },
        {
            "name": "record_cycle_summary",
            "description": (
                "Persist the reasoning-stage summary and proposed next decision for "
                "the current cycle. This is an index record; selection/question "
                "state is still committed by the core reasoning transition."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["conclusion_md", "decision", "next_step_md"],
                "additionalProperties": False,
                "properties": {
                    "conclusion_md": {"type": "string", "minLength": 1, "maxLength": 262144},
                    "decision": {
                        "enum": ["continue", "decompose", "replan", "terminate", "inconclusive"]
                    },
                    "next_step_md": {"type": "string", "maxLength": 65536},
                    "evidence_refs": {
                        "type": "array", "maxItems": 256,
                        "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    },
                },
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "submit_stage_artifact",
            "description": (
                "Validate and durably submit the current main stage artifact. "
                "Schema, stage file closure, current cycle/target identity and "
                "Bundle plan binding errors are returned immediately in this "
                "same Codex turn. On success the file manager stores payloads "
                "and SQLite records only the receipt path/hash. Revise and call "
                "again after any error; do not end the stage before success."
            ),
            "inputSchema": {
                "type": "object", "required": ["files"],
                "additionalProperties": False,
                "properties": {
                    "files": {"type": "object", "maxProperties": 1024},
                    "workspace_files": {
                        "type": "array", "maxItems": 1024,
                        "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    },
                    "md": {"type": "string", "maxLength": 262144},
                },
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": False},
        },
    ]


@dataclass(frozen=True)
class RuntimeMCPScope:
    cycle_id: str
    stage: str
    target_id: Optional[str]
    purpose: str
    expires_at: Optional[float]
    pack_hash: str = ""
    refs: tuple[str, ...] = ()
    workspace_root: Optional[str] = None
    output_uid: Optional[int] = None
    runner_call_id: Optional[int] = None
    native_review_ledger: Optional[NativeReviewLedger] = None


def _bundle_role_for_scope(scope: RuntimeMCPScope) -> Optional[str]:
    if scope.stage != "bundle":
        return None
    if scope.purpose.startswith("bundle-scheduler-c"):
        return "scheduler"
    if scope.purpose.startswith("bundle-worker-c"):
        return "worker"
    return "legacy"


def _tool_definitions_for_scope(
        scope: RuntimeMCPScope) -> list[Dict[str, Any]]:
    """Expose only the tools represented by this live Bundle capability."""
    role = _bundle_role_for_scope(scope)
    if role == "scheduler":
        allowed = _BUNDLE_SCHEDULER_TOOLS
    elif role == "worker":
        allowed = _BUNDLE_WORKER_TOOLS
    else:
        return _tool_definitions()
    return [
        definition for definition in _tool_definitions()
        if definition["name"] in allowed
    ]


class RuntimeMCPError(RuntimeError):
    """A user-actionable persistence rejection returned to the live Codex turn."""


def _cycle_number(cycle_id: str) -> int:
    if not isinstance(cycle_id, str) or re.fullmatch(r"c[1-9][0-9]*", cycle_id) is None:
        raise RuntimeMCPError(f"cycle_id 非法: {cycle_id!r}")
    return int(cycle_id[1:])


def _bounded_text(value: Any, *, name: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise RuntimeMCPError(f"{name} 须为{'可空' if allow_empty else '非空'}字符串")
    if len(value.encode("utf-8")) > maximum:
        raise RuntimeMCPError(f"{name} 超过 {maximum} bytes")
    return value


def _card_hash(card_md: str) -> str:
    return "sha256:" + hashlib.sha256(card_md.encode("utf-8")).hexdigest()


def _strict_json_object(raw: Any, *, label: str) -> Dict[str, Any]:
    def unique(pairs):  # noqa: ANN001 - json object_pairs_hook protocol
        value: Dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise RuntimeMCPError(f"{label} 含重复 JSON key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw, object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                RuntimeMCPError(f"{label} 含非法 JSON constant: {token}")))
    except RuntimeMCPError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeMCPError(f"{label} 不是严格 JSON object") from error
    if not isinstance(value, dict):
        raise RuntimeMCPError(f"{label} 须为 JSON object")
    return value


class RuntimeIngestService:
    """Small domain API used by the MCP broker; all writes use one WriteDaemon."""

    def __init__(self, daemon, *, schemas=None, policy=None, work_root=None,
                 owner_guard=None, wildidea_adapter=None):  # noqa: ANN001
        self.daemon = daemon
        self.schemas = schemas
        self.policy = policy
        self.work_root = (
            None if work_root is None else Path(work_root).resolve(strict=False))
        self.owner_guard = owner_guard or (lambda: None)
        self.wildidea_adapter = wildidea_adapter
        self.plan_controller = None
        self.reasoning_controller = None
        self.bundle_controller = None

    def bind_plan_controller(self, controller) -> None:  # noqa: ANN001
        """Bind trusted Plan-side tools without giving the model DB authority."""
        required = ("preflight_plan_session", "run_plan_import_search")
        if controller is None or not all(
                callable(getattr(controller, name, None)) for name in required):
            raise ValueError(
                "plan controller 缺 preflight/import-search capability")
        if self.plan_controller is not None and self.plan_controller is not controller:
            raise RuntimeError("runtime MCP plan controller 不得重绑定")
        self.plan_controller = controller

    def bind_reasoning_controller(self, controller) -> None:  # noqa: ANN001
        """Bind the exact dry-run of the final Reasoning core transaction."""
        if controller is None or not callable(
                getattr(controller, "preflight_reasoning_session", None)):
            raise ValueError("reasoning controller 缺 semantic preflight capability")
        if (self.reasoning_controller is not None
                and self.reasoning_controller is not controller):
            raise RuntimeError("runtime MCP reasoning controller 不得重绑定")
        self.reasoning_controller = controller

    def bind_bundle_controller(self, controller) -> None:  # noqa: ANN001
        """Bind the owner-side Scheduler and fixed-target Worker pipeline."""
        worker_methods = (
            "bundle_session_scope", "execute_bundle_session",
            "bundle_session_status", "request_bundle_repair",
            "replan_bundle_session",
        )
        scheduler_methods = (
            "bundle_scheduler_overview", "dispatch_bundle_frontier",
            "wait_bundle_scheduler", "drain_bundle_scheduler",
        )
        has_worker = (
            controller is not None
            and all(callable(getattr(controller, name, None))
                    for name in worker_methods)
        )
        has_scheduler = (
            controller is not None
            and all(callable(getattr(controller, name, None))
                    for name in scheduler_methods)
        )
        # A legacy controller remains loadable only so an already-running
        # pre-DAG Bundle task can be recovered and drained. New production
        # assembly supplies the Scheduler methods and never uses this branch.
        has_legacy = (
            controller is not None
            and callable(getattr(controller, "bind_next_bundle_target", None))
        )
        if not has_worker or not (has_scheduler or has_legacy):
            raise ValueError(
                "bundle controller 缺 Scheduler overview/dispatch/wait/drain "
                "或 fixed-target Worker scope/execute/status/repair/replan capability")
        if self.bundle_controller is not None and self.bundle_controller is not controller:
            raise RuntimeError("runtime MCP bundle controller 不得重绑定")
        self.bundle_controller = controller

    @staticmethod
    def tools() -> list[Dict[str, Any]]:
        return _tool_definitions()

    def call(
            self, scope: RuntimeMCPScope, name: str,
            arguments: Mapping[str, Any], *,
            submission_commit_fence: Optional[
                Callable[[], ContextManager[Callable[[], None]]]] = None,
            ) -> Dict[str, Any]:
        self.owner_guard()
        if not isinstance(arguments, Mapping):
            raise RuntimeMCPError("MCP tool arguments 须为 object")
        handlers = {
            "get_runtime_index": self._get_runtime_index,
            "preflight_plan": self._preflight_plan,
            "plan_import_search": self._plan_import_search,
            "wildidea_expand": self._wildidea_expand,
            "wildidea_search": self._wildidea_search,
            "wildidea_audit": self._wildidea_audit,
            "bundle_overview": self._bundle_overview,
            "bundle_dispatch": self._bundle_dispatch,
            "bundle_wait": self._bundle_wait,
            "bundle_drain": self._bundle_drain,
            "bundle_next_target": self._bundle_next_target,
            "bundle_execute": self._bundle_execute,
            "bundle_status": self._bundle_status,
            "bundle_repair": self._bundle_repair,
            "bundle_replan": self._bundle_replan,
            "upsert_card": self._upsert_card,
            "prepare_review": self._prepare_review,
            "read_review_input": self._read_review_input,
            "record_review": self._record_review,
            "record_cycle_summary": self._record_cycle_summary,
            "submit_stage_artifact": self._submit_stage_artifact,
        }
        handler = handlers.get(name)
        if handler is None:
            raise RuntimeMCPError(f"未知 runtime MCP tool: {name!r}")
        self._assert_bundle_capability(scope, name)
        if (scope.stage == "bundle" and name in {
                "submit_stage_artifact", "prepare_review", "read_review_input",
                "record_review", "bundle_execute", "bundle_status",
                "bundle_repair", "bundle_replan"}):
            scope = self._effective_bundle_scope(scope)
        if name in {
                "wildidea_expand", "wildidea_audit",
                "submit_stage_artifact", "prepare_review", "record_review"}:
            return handler(
                scope, dict(arguments), commit_fence=submission_commit_fence)
        return handler(scope, dict(arguments))

    @staticmethod
    def _bundle_role(scope: RuntimeMCPScope) -> Optional[str]:
        return _bundle_role_for_scope(scope)

    @classmethod
    def _assert_bundle_capability(
            cls, scope: RuntimeMCPScope, name: str) -> None:
        """Keep graph control and target mutation in disjoint trusted grants."""
        role = cls._bundle_role(scope)
        if role is None or role == "legacy":
            return
        if role == "scheduler":
            if scope.target_id is not None:
                raise RuntimeMCPError(
                    "Scheduler capability 不得绑定 build_target")
            if name not in _BUNDLE_SCHEDULER_TOOLS:
                raise RuntimeMCPError(
                    f"Scheduler capability 不允许调用 {name}")
            return
        if scope.target_id is None:
            raise RuntimeMCPError(
                "Target Worker capability 必须固定绑定一个 build_target")
        if name not in _BUNDLE_WORKER_TOOLS:
            raise RuntimeMCPError(
                f"Target Worker capability 不允许调用 {name}")

    def _require_bundle_scheduler(self, scope: RuntimeMCPScope):  # noqa: ANN201
        controller = self._require_bundle_controller(scope)
        if self._bundle_role(scope) != "scheduler" or scope.target_id is not None:
            raise RuntimeMCPError(
                "Bundle Scheduler tool 要求 cycle-scoped Scheduler capability")
        return controller

    def _bundle_overview(
            self, scope: RuntimeMCPScope,
            arguments: Dict[str, Any]) -> Dict[str, Any]:
        if arguments:
            raise RuntimeMCPError("bundle_overview 不接受参数")
        controller = self._require_bundle_scheduler(scope)
        try:
            result = controller.bundle_scheduler_overview(scope)
        except Exception as error:
            raise RuntimeMCPError(str(error)) from error
        if not isinstance(result, dict):
            raise RuntimeMCPError("Bundle Scheduler overview 返回非 object")
        return {"ok": True, **result}

    def _bundle_dispatch(
            self, scope: RuntimeMCPScope,
            arguments: Dict[str, Any]) -> Dict[str, Any]:
        if arguments:
            raise RuntimeMCPError("bundle_dispatch 不接受参数")
        controller = self._require_bundle_scheduler(scope)
        try:
            result = controller.dispatch_bundle_frontier(scope)
        except Exception as error:
            raise RuntimeMCPError(str(error)) from error
        if not isinstance(result, dict):
            raise RuntimeMCPError("Bundle Scheduler dispatch 返回非 object")
        return {"ok": True, **result}

    def _bundle_wait(
            self, scope: RuntimeMCPScope,
            arguments: Dict[str, Any]) -> Dict[str, Any]:
        if set(arguments) != {"after_revision", "timeout_s"}:
            raise RuntimeMCPError(
                "bundle_wait 须且只须提供 after_revision/timeout_s")
        revision = arguments.get("after_revision")
        timeout_s = arguments.get("timeout_s")
        if (isinstance(revision, bool) or not isinstance(revision, int)
                or revision < 0):
            raise RuntimeMCPError("after_revision 须为非负整数")
        if (isinstance(timeout_s, bool)
                or not isinstance(timeout_s, (int, float))
                or not math.isfinite(float(timeout_s))
                or not 0 <= float(timeout_s) <= 1800):
            raise RuntimeMCPError("timeout_s 须为 [0,1800] 有限数")
        controller = self._require_bundle_scheduler(scope)
        try:
            result = controller.wait_bundle_scheduler(
                scope, after_revision=revision, timeout_s=float(timeout_s))
        except Exception as error:
            raise RuntimeMCPError(str(error)) from error
        if not isinstance(result, dict):
            raise RuntimeMCPError("Bundle Scheduler wait 返回非 object")
        return {"ok": True, **result}

    def _bundle_drain(
            self, scope: RuntimeMCPScope,
            arguments: Dict[str, Any]) -> Dict[str, Any]:
        if arguments:
            raise RuntimeMCPError("bundle_drain 不接受参数")
        controller = self._require_bundle_scheduler(scope)
        try:
            result = controller.drain_bundle_scheduler(scope)
        except Exception as error:
            raise RuntimeMCPError(str(error)) from error
        if not isinstance(result, dict):
            raise RuntimeMCPError("Bundle Scheduler drain 返回非 object")
        return {"ok": True, **result}

    def _plan_import_search(self, scope: RuntimeMCPScope,
                            arguments: Dict[str, Any]) -> Dict[str, Any]:
        if scope.stage != "plan":
            raise RuntimeMCPError(
                "plan_import_search 只允许 resident Plan 主阶段调用")
        if set(arguments) != {"request"} or not isinstance(
                arguments.get("request"), dict):
            raise RuntimeMCPError(
                "plan_import_search 须且只须提供 request object")
        if self.plan_controller is None:
            raise RuntimeMCPError("当前运行时未装配 Plan import-search controller")
        try:
            result = self.plan_controller.run_plan_import_search(
                scope, arguments["request"])
        except Exception as error:
            raise RuntimeMCPError(f"Plan import search 失败: {error}") from error
        if not isinstance(result, dict):
            raise RuntimeMCPError("Plan import-search controller 返回非 object")
        return {"ok": True, **result}

    def _require_bundle_controller(self, scope: RuntimeMCPScope):  # noqa: ANN201
        if scope.stage != "bundle":
            raise RuntimeMCPError("Bundle session tool 只允许 Bundle 主阶段调用")
        if self.bundle_controller is None:
            raise RuntimeMCPError("当前运行时未装配官方 Bundle session controller")
        return self.bundle_controller

    def _effective_bundle_scope(self, scope: RuntimeMCPScope) -> RuntimeMCPScope:
        """Project the cycle-wide capability onto its currently bound target."""
        controller = self._require_bundle_controller(scope)
        try:
            binding = controller.bundle_session_scope(scope)
        except Exception as error:
            raise RuntimeMCPError(str(error)) from error
        if (not isinstance(binding, Mapping)
                or set(binding) != {"target_id", "pack_hash", "refs"}):
            raise RuntimeMCPError("Bundle controller target scope 结构非法")
        target_id = str(binding["target_id"])
        if re.fullmatch(r"[1-9][0-9]*", target_id) is None:
            raise RuntimeMCPError("Bundle controller target_id 非法")
        pack_hash = binding["pack_hash"]
        refs = binding["refs"]
        if (not isinstance(pack_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", pack_hash) is None
                or not isinstance(refs, (list, tuple))
                or any(not isinstance(ref, str) for ref in refs)):
            raise RuntimeMCPError("Bundle controller ContextPack 绑定非法")
        return replace(
            scope, target_id=target_id, pack_hash=pack_hash, refs=tuple(refs))

    def _bundle_next_target(self, scope: RuntimeMCPScope,
                            arguments: Dict[str, Any]) -> Dict[str, Any]:
        if arguments:
            raise RuntimeMCPError("bundle_next_target 不接受参数")
        controller = self._require_bundle_controller(scope)
        current_scope = None
        try:
            current_scope = self._effective_bundle_scope(scope)
        except RuntimeMCPError as error:
            # The first call in a cycle-wide Bundle turn has no controller
            # binding yet.  Other controller-scope failures remain fatal.
            message = str(error)
            if ("尚未绑定 target" not in message
                    and "call next first" not in message):
                raise
        if current_scope is not None:
            row = self.daemon.query_one(
                "SELECT cycle_id,status FROM build_target WHERE id=?",
                (int(current_scope.target_id),))
            if (row is None
                    or int(row[0]) != _cycle_number(scope.cycle_id)):
                raise RuntimeMCPError(
                    "Bundle controller 当前 target 不属于当前 cycle")
            if row[1] == "complete":
                with self.daemon.transaction() as conn:
                    self._require_bundle_result_review_ack_in_txn(
                        conn, current_scope)
        try:
            result = controller.bind_next_bundle_target(scope)
        except Exception as error:
            raise RuntimeMCPError(str(error)) from error
        if not isinstance(result, dict):
            raise RuntimeMCPError("Bundle controller next target 返回非 object")
        return {"ok": True, **result}

    def _exact_bundle_submission(
            self, scope: RuntimeMCPScope, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if set(arguments) != {"submission_ref", "submission_hash"}:
            raise RuntimeMCPError(
                "bundle_execute 须且只须提供 submission_ref/submission_hash")
        reference = _bounded_text(
            arguments.get("submission_ref"), name="submission_ref", maximum=4096)
        digest = arguments.get("submission_hash")
        if (not isinstance(digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None):
            raise RuntimeMCPError("submission_hash 非法")
        loaded = self.load_stage_submission(reference, digest)
        try:
            receipt = json.loads(Path(reference).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeMCPError("Bundle submission receipt 不可复验") from error
        if (receipt.get("cycle_id") != scope.cycle_id
                or receipt.get("stage") != "bundle"
                or str(receipt.get("target_id")) != str(scope.target_id)
                or receipt.get("purpose") != scope.purpose
                or receipt.get("submission_kind") != "bundle"
                or receipt.get("pack_hash") != (scope.pack_hash or None)):
            raise RuntimeMCPError("Bundle submission 与当前 capability 身份不一致")
        return loaded

    def _bundle_execute(self, scope: RuntimeMCPScope,
                        arguments: Dict[str, Any]) -> Dict[str, Any]:
        controller = self._require_bundle_controller(scope)
        loaded = self._exact_bundle_submission(scope, arguments)
        current = controller.bundle_session_status(scope)
        if (isinstance(current, Mapping)
                and current.get("awaiting_result_review") is True):
            raise RuntimeMCPError(
                "当前 result candidate 尚未完成独立审查；"
                "请先 prepare_review/record_review，或选择 bundle_repair/"
                "bundle_replan")
        if (isinstance(current, Mapping)
                and current.get("result_review_ready") is True):
            # Admission resumes only through this owner-side proof check.  The
            # controller status intentionally exposes a compact projection;
            # it is not itself authority for the durable child/request/
            # provider/guardian lineage.
            with self.daemon.transaction() as conn:
                self._require_bundle_result_review_ack_in_txn(conn, scope)
        result = controller.execute_bundle_session(scope, loaded["files"])
        if not isinstance(result, dict):
            raise RuntimeMCPError("Bundle controller 返回非 object")
        return {"ok": True, **result}

    def _bundle_status(self, scope: RuntimeMCPScope,
                       arguments: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "mode", "after_seq", "after_status_revision",
            "limit", "timeout_s"}
        if set(arguments) - allowed:
            raise RuntimeMCPError(
                "bundle_status 只接受 mode/after_seq/"
                "after_status_revision/limit/timeout_s")
        mode = arguments.get("mode", "incremental")
        after_seq = arguments.get("after_seq", 0)
        after_status_revision = arguments.get(
            "after_status_revision", 0)
        limit = arguments.get("limit", 200)
        timeout_s = arguments.get("timeout_s", 0)
        if mode not in {"snapshot", "incremental"}:
            raise RuntimeMCPError(
                "bundle_status.mode 须为 snapshot 或 incremental")
        if (isinstance(after_seq, bool) or not isinstance(after_seq, int)
                or after_seq < 0):
            raise RuntimeMCPError("bundle_status.after_seq 须为非负整数")
        if (
            isinstance(after_status_revision, bool)
            or not isinstance(after_status_revision, int)
            or after_status_revision < 0
        ):
            raise RuntimeMCPError(
                "bundle_status.after_status_revision 须为非负整数")
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= 1000):
            raise RuntimeMCPError("bundle_status.limit 须为 [1,1000] 整数")
        if (isinstance(timeout_s, bool)
                or not isinstance(timeout_s, (int, float))
                or not math.isfinite(float(timeout_s))
                or not 0 <= float(timeout_s) <= 1800):
            raise RuntimeMCPError(
                "bundle_status.timeout_s 须为 [0,1800] 有限数")
        controller = self._require_bundle_controller(scope)
        if arguments:
            result = controller.bundle_session_status(
                scope, mode=mode, after_seq=after_seq,
                after_status_revision=after_status_revision,
                limit=limit, timeout_s=float(timeout_s))
        else:
            # Compatibility for a pre-DAG in-flight controller whose method
            # predates cursor-aware monitoring. New Worker calls always pass
            # the explicit bounded arguments from the Worker skill.
            result = controller.bundle_session_status(scope)
        if not isinstance(result, dict):
            raise RuntimeMCPError("Bundle controller status 返回非 object")
        return {"ok": True, **result}

    def _bundle_repair(self, scope: RuntimeMCPScope,
                       arguments: Dict[str, Any]) -> Dict[str, Any]:
        controller = self._require_bundle_controller(scope)
        if set(arguments) != {"diagnosis_md"}:
            raise RuntimeMCPError("bundle_repair 须且只须提供 diagnosis_md")
        diagnosis = _bounded_text(
            arguments.get("diagnosis_md"), name="diagnosis_md", maximum=8192)
        result = controller.request_bundle_repair(scope, diagnosis)
        if not isinstance(result, dict):
            raise RuntimeMCPError("Bundle controller repair 返回非 object")
        return {"ok": True, **result}

    def _bundle_replan(self, scope: RuntimeMCPScope,
                       arguments: Dict[str, Any]) -> Dict[str, Any]:
        controller = self._require_bundle_controller(scope)
        if set(arguments) != {"diagnosis_md"}:
            raise RuntimeMCPError("bundle_replan 须且只须提供 diagnosis_md")
        diagnosis = _bounded_text(
            arguments.get("diagnosis_md"), name="diagnosis_md", maximum=8192)
        result = controller.replan_bundle_session(scope, diagnosis)
        if not isinstance(result, dict):
            raise RuntimeMCPError("Bundle controller replan 返回非 object")
        return {"ok": True, **result}

    def _cycle_row(self, scope: RuntimeMCPScope) -> tuple[int, int, Optional[int], str]:
        ci = _cycle_number(scope.cycle_id)
        row = self.daemon.query_one(
            "SELECT goal_id,goal_ver,active_question_id,status FROM cycle WHERE id=?", (ci,))
        if row is None:
            raise RuntimeMCPError(f"当前 cycle 不存在: {scope.cycle_id}")
        if row[3] in {"done", "failed", "aborted"}:
            raise RuntimeMCPError(f"cycle {scope.cycle_id} 已终态，拒绝 runtime 写入")
        return int(row[0]), int(row[1]), row[2], str(row[3])

    def _get_runtime_index(self, scope: RuntimeMCPScope,
                           arguments: Dict[str, Any]) -> Dict[str, Any]:
        if arguments:
            raise RuntimeMCPError("get_runtime_index 不接受参数")
        goal_id, goal_ver, active_q, status = self._cycle_row(scope)
        baselines = [
            {
                "id": row[0], "canonical_key": row[1], "slug": row[2],
                "status": row[3], "born_cycle": row[4],
            }
            for row in self.daemon.query(
                "SELECT id,canonical_key,slug,status,born_cycle FROM baseline "
                "ORDER BY id DESC LIMIT 100")
        ]
        cards = [
            {
                "id": row[0], "card_type": row[1], "ref_id": row[2],
                "src_hash": row[3], "stale": bool(row[4]),
            }
            for row in self.daemon.query(
                "SELECT id,card_type,ref_id,src_hash,stale FROM card "
                "WHERE goal_id=? AND goal_ver=? ORDER BY id DESC LIMIT 200",
                (goal_id, goal_ver))
        ]
        question = None
        if active_q is not None:
            qrow = self.daemon.query_one(
                "SELECT id,text,status,visit_count FROM question WHERE id=?", (active_q,))
            if qrow is not None:
                question = {
                    "id": qrow[0], "text": qrow[1], "status": qrow[2],
                    "visit_count": qrow[3],
                }
        return {
            "ok": True, "cycle_id": scope.cycle_id, "cycle_status": status,
            "stage": scope.stage, "target_id": scope.target_id,
            "goal_id": goal_id, "goal_ver": goal_ver,
            "active_question": question, "baselines": baselines, "cards": cards,
        }

    def _preflight_plan(self, scope: RuntimeMCPScope,
                        arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Validate a complete Plan proposal without claiming core identity."""
        if scope.stage != "plan":
            raise RuntimeMCPError("preflight_plan 只允许 Plan 主阶段调用")
        if set(arguments) != {"plan"} or not isinstance(arguments.get("plan"), dict):
            raise RuntimeMCPError("preflight_plan 须且只须提供 plan object")
        plan = arguments["plan"]
        self._validate_stage_files(scope, {"plan.json": plan})
        claims = []
        for target in plan.get("targets", []):
            if isinstance(target, dict) and target.get("target_kind") == "build":
                claim = target.get("claim") or {}
                claims.append({
                    "target_key": target.get("target_key"),
                    "canonical_key": claim.get("canonical_key"),
                    "slug": claim.get("slug"),
                })
        index = self._get_runtime_index(scope, {})
        resources = json.loads(json.dumps(
            (self.policy or {}).get("resources") or {},
            ensure_ascii=False, sort_keys=True))
        return {
            "ok": True,
            "cycle_id": scope.cycle_id,
            "baseline_claims": claims,
            "resources": resources,
            "runtime_index": index,
            "writes_performed": 0,
        }

    @staticmethod
    def _validate_idea_route_receipt(payload: Dict[str, Any]) -> None:
        required = {
            "protocol", "cycle_id", "stage", "target_id", "purpose",
            "pack_hash", "runner_call_id", "need_innovation",
            "generation_path", "engine_version", "adapter_version",
            "expand_result_hash", "receipt_hash",
        }
        if (set(payload) != required
                or payload.get("protocol")
                != "runtime-idea-generation-path-v1"):
            raise RuntimeMCPError(
                "Idea generation_path receipt 字段闭包/协议非法")
        if (payload.get("stage") != "idea"
                or payload.get("target_id") is not None
                or not isinstance(payload.get("need_innovation"), bool)):
            raise RuntimeMCPError(
                "Idea generation_path receipt 阶段/分支非法")
        expected_path = (
            "wildidea" if payload["need_innovation"] else "bypass")
        if payload.get("generation_path") != expected_path:
            raise RuntimeMCPError(
                "Idea generation_path receipt 分支自相矛盾")
        if (_SHA256.fullmatch(str(payload.get("expand_result_hash") or ""))
                is None
                or _SHA256.fullmatch(str(payload.get("receipt_hash") or ""))
                is None):
            raise RuntimeMCPError(
                "Idea generation_path receipt hash 非法")
        if (not isinstance(payload.get("pack_hash"), str)
                or re.fullmatch(r"[0-9a-f]{64}", payload["pack_hash"]) is None):
            raise RuntimeMCPError(
                "Idea generation_path receipt pack_hash 非法")
        if (isinstance(payload.get("runner_call_id"), bool)
                or not isinstance(payload.get("runner_call_id"), int)
                or payload["runner_call_id"] <= 0):
            raise RuntimeMCPError(
                "Idea generation_path receipt runner_call_id 非法")
        for field in (
                "cycle_id", "purpose", "engine_version", "adapter_version"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise RuntimeMCPError(
                    f"Idea generation_path receipt {field} 非法")
        if RuntimeIngestService._receipt_hash(payload) != payload["receipt_hash"]:
            raise RuntimeMCPError(
                "Idea generation_path receipt hash 不可复验")

    def _idea_route_in_txn(
            self, conn: sqlite3.Connection, scope: RuntimeMCPScope,
            ) -> Optional[tuple[int, Dict[str, Any]]]:
        rows = conn.execute(
            "SELECT id,payload_json FROM decision "
            "WHERE cycle_id=? AND type='runtime_idea_generation_path' "
            "ORDER BY id",
            (_cycle_number(scope.cycle_id),)).fetchall()
        selected = []
        for decision_id, raw in rows:
            payload = _strict_json_object(
                raw, label="Idea generation_path decision")
            self._validate_idea_route_receipt(payload)
            if (
                    payload["cycle_id"] == scope.cycle_id
                    and payload["stage"] == scope.stage
                    and payload["target_id"] == scope.target_id
                    and payload["purpose"] == scope.purpose
                    and payload["pack_hash"] == scope.pack_hash
                    and payload["runner_call_id"] == scope.runner_call_id):
                selected.append((int(decision_id), payload))
        if len(selected) > 1:
            raise RuntimeMCPError(
                "当前 Idea 主 turn 存在重复 generation_path binding")
        return selected[0] if selected else None

    @staticmethod
    def _assert_idea_matches_route(
            idea_set: Mapping[str, Any], route: Mapping[str, Any]) -> None:
        expected_need = route["need_innovation"]
        expected_path = route["generation_path"]
        if idea_set.get("need_innovation") is not expected_need:
            raise RuntimeMCPError(
                "idea_set.need_innovation 与服务端 generation_path="
                f"{expected_path} 不一致")
        candidates = idea_set.get("candidates")
        if (not isinstance(candidates, list) or not candidates
                or any(not isinstance(candidate, Mapping)
                       or candidate.get("generation_path") != expected_path
                       for candidate in candidates)):
            raise RuntimeMCPError(
                "idea_set candidates 与服务端 "
                f"generation_path={expected_path} 不一致")

    def _wildidea_expand(
            self, scope: RuntimeMCPScope, arguments: Dict[str, Any], *,
            commit_fence: Optional[
                Callable[[], ContextManager[Callable[[], None]]]] = None,
            ) -> Dict[str, Any]:
        if scope.stage != "idea":
            raise RuntimeMCPError("wildidea_expand 只允许 Idea 主阶段调用")
        if set(arguments) != {"need_innovation"} or not isinstance(
                arguments.get("need_innovation"), bool):
            raise RuntimeMCPError("wildidea_expand 须且只须提供 need_innovation boolean")
        if self.wildidea_adapter is None:
            raise RuntimeMCPError("当前运行时未装配 WildIdea capability")
        if scope.runner_call_id is None:
            raise RuntimeMCPError(
                "resident WildIdea 缺当前主 runner_call 服务端身份")
        requested_need = arguments["need_innovation"]
        with self.daemon.transaction() as conn:
            prior = self._idea_route_in_txn(conn, scope)
        if prior is not None and prior[1]["need_innovation"] is not requested_need:
            raise RuntimeMCPError(
                "当前 Idea 主 turn 的 generation_path 已由服务端绑定，不得重绑定")
        try:
            result = self.wildidea_adapter.resident_expand(
                scope, need_innovation=requested_need)
        except Exception as error:
            raise RuntimeMCPError(f"WildIdea expansion 失败: {error}") from error
        if not isinstance(result, dict):
            raise RuntimeMCPError("WildIdea expansion controller 返回非 object")
        expected_path = "wildidea" if requested_need else "bypass"
        if (result.get("need_innovation") is not requested_need
                or result.get("generation_path") != expected_path):
            raise RuntimeMCPError(
                "WildIdea expansion controller 分支身份漂移")
        result_hash = "sha256:" + hashlib.sha256(
            self._canonical_bytes(result)).hexdigest()
        metadata = getattr(self.wildidea_adapter, "metadata", None)
        if not isinstance(metadata, Mapping):
            raise RuntimeMCPError("WildIdea adapter metadata 非法")
        engine_version = metadata.get("engine_version")
        adapter_version = metadata.get("adapter_version")
        payload = {
            "protocol": "runtime-idea-generation-path-v1",
            "cycle_id": scope.cycle_id, "stage": scope.stage,
            "target_id": scope.target_id, "purpose": scope.purpose,
            "pack_hash": scope.pack_hash,
            "runner_call_id": scope.runner_call_id,
            "need_innovation": requested_need,
            "generation_path": expected_path,
            "engine_version": engine_version,
            "adapter_version": adapter_version,
            "expand_result_hash": result_hash,
        }
        payload["receipt_hash"] = self._receipt_hash(payload)
        self._validate_idea_route_receipt(payload)
        if prior is not None:
            if prior[1] != payload:
                raise RuntimeMCPError(
                    "WildIdea expansion 重放与既有服务端回执不一致")
            return {
                "ok": True, **result,
                "decision_id": prior[0],
                "route_receipt_hash": payload["receipt_hash"],
            }
        fence = (
            commit_fence() if commit_fence is not None
            else nullcontext(lambda: None))
        with fence as assert_commit_authorized:
            with self.daemon.transaction() as conn:
                concurrent = self._idea_route_in_txn(conn, scope)
                if concurrent is not None:
                    if concurrent[1] != payload:
                        raise RuntimeMCPError(
                            "WildIdea generation_path 并发重绑定")
                    decision_id = concurrent[0]
                else:
                    decision_id = conn.execute(
                        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                        "VALUES (?,'orchestrator',"
                        "'runtime_idea_generation_path',?)",
                        (_cycle_number(scope.cycle_id), json.dumps(
                            payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")))).lastrowid
                assert_commit_authorized()
        return {
            "ok": True, **result,
            "decision_id": int(decision_id),
            "route_receipt_hash": payload["receipt_hash"],
        }

    @staticmethod
    def _validate_wildidea_result_receipt(payload: Dict[str, Any]) -> None:
        required = {
            "protocol", "cycle_id", "stage", "target_id", "purpose",
            "pack_hash", "runner_call_id", "route_decision_id",
            "route_receipt_hash", "generation_path", "artifact_hash",
            "engine_version", "adapter_version",
            "internal_provenance", "internal_provenance_hash",
            "judge_runner_call_id",
            "judge_provider_receipt_hash", "receipt_hash",
        }
        if (set(payload) != required
                or payload.get("protocol") != "runtime-wildidea-result-v1"
                or payload.get("stage") != "idea"
                or payload.get("target_id") is not None
                or payload.get("generation_path") != "wildidea"):
            raise RuntimeMCPError(
                "WildIdea internal result receipt 字段闭包/协议非法")
        for field in (
                "route_receipt_hash", "artifact_hash",
                "internal_provenance_hash", "receipt_hash"):
            if _SHA256.fullmatch(str(payload.get(field) or "")) is None:
                raise RuntimeMCPError(
                    f"WildIdea internal result receipt {field} 非法")
        optional_judge_hash = payload.get("judge_provider_receipt_hash")
        if (optional_judge_hash is not None
                and _SHA256.fullmatch(str(optional_judge_hash)) is None):
            raise RuntimeMCPError(
                "WildIdea internal result receipt judge receipt hash 非法")
        for field in (
                "runner_call_id", "route_decision_id"):
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RuntimeMCPError(
                    f"WildIdea internal result receipt {field} 非法")
        judge_call = payload.get("judge_runner_call_id")
        if (judge_call is not None and (
                isinstance(judge_call, bool)
                or not isinstance(judge_call, int) or judge_call <= 0)):
            raise RuntimeMCPError(
                "WildIdea internal result receipt judge_runner_call_id 非法")
        for field in (
                "cycle_id", "purpose", "pack_hash",
                "engine_version", "adapter_version"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise RuntimeMCPError(
                    f"WildIdea internal result receipt {field} 非法")
        provenance = payload.get("internal_provenance")
        if not isinstance(provenance, dict):
            raise RuntimeMCPError(
                "WildIdea internal result receipt provenance 非法")
        provenance_hash = "sha256:" + hashlib.sha256(
            RuntimeIngestService._canonical_bytes(provenance)).hexdigest()
        if provenance_hash != payload["internal_provenance_hash"]:
            raise RuntimeMCPError(
                "WildIdea internal result receipt provenance hash 不可复验")
        if RuntimeIngestService._receipt_hash(payload) != payload["receipt_hash"]:
            raise RuntimeMCPError(
                "WildIdea internal result receipt hash 不可复验")

    def _wildidea_result_in_txn(
            self, conn: sqlite3.Connection, scope: RuntimeMCPScope,
            route_decision_id: int, route_receipt_hash: str,
            ) -> Optional[tuple[int, Dict[str, Any]]]:
        rows = conn.execute(
            "SELECT id,payload_json FROM decision "
            "WHERE cycle_id=? AND type='runtime_wildidea_result' ORDER BY id",
            (_cycle_number(scope.cycle_id),)).fetchall()
        selected = []
        for decision_id, raw in rows:
            payload = _strict_json_object(
                raw, label="WildIdea internal result decision")
            self._validate_wildidea_result_receipt(payload)
            if (
                    payload["cycle_id"] == scope.cycle_id
                    and payload["stage"] == scope.stage
                    and payload["target_id"] == scope.target_id
                    and payload["purpose"] == scope.purpose
                    and payload["pack_hash"] == scope.pack_hash
                    and payload["runner_call_id"] == scope.runner_call_id
                    and payload["route_decision_id"] == route_decision_id
                    and payload["route_receipt_hash"] == route_receipt_hash):
                selected.append((int(decision_id), payload))
        if len(selected) > 1:
            raise RuntimeMCPError(
                "当前 Idea 主 turn 存在重复 WildIdea internal result")
        return selected[0] if selected else None

    def _wildidea_audit(
            self, scope: RuntimeMCPScope, arguments: Dict[str, Any], *,
            commit_fence: Optional[
                Callable[[], ContextManager[Callable[[], None]]]] = None,
            ) -> Dict[str, Any]:
        if scope.stage != "idea":
            raise RuntimeMCPError("wildidea_audit 只允许 Idea 主阶段调用")
        if set(arguments) != {"draft"} or not isinstance(
                arguments.get("draft"), dict):
            raise RuntimeMCPError("wildidea_audit 须且只须提供 draft object")
        if self.wildidea_adapter is None:
            raise RuntimeMCPError("当前运行时未装配 WildIdea capability")
        with self.daemon.transaction() as conn:
            route_row = self._idea_route_in_txn(conn, scope)
            if route_row is None:
                raise RuntimeMCPError(
                    "wildidea_audit 前必须先调用 wildidea_expand 绑定路径")
            route_decision_id, route = route_row
            if route["generation_path"] != "wildidea":
                raise RuntimeMCPError(
                    "wildidea_audit 只允许服务端绑定的 WildIdea 路径")
            existing = self._wildidea_result_in_txn(
                conn, scope, route_decision_id, route["receipt_hash"])
            if existing is not None:
                raise RuntimeMCPError(
                    "当前 WildIdea internal audit 已完成，不得重复运行")
        try:
            controlled = self.wildidea_adapter.resident_audit(
                scope, draft=arguments["draft"])
        except Exception as error:
            raise RuntimeMCPError(
                f"WildIdea internal audit 失败: {error}") from error
        if (not isinstance(controlled, dict)
                or set(controlled) != {
                    "idea_set", "internal_provenance"}
                or not isinstance(controlled.get("idea_set"), dict)
                or not isinstance(
                    controlled.get("internal_provenance"), dict)):
            raise RuntimeMCPError(
                "WildIdea internal audit controller 返回字段闭包非法")
        idea_set = controlled["idea_set"]
        self._require_schema("idea_set.json", idea_set)
        self._assert_idea_matches_route(idea_set, route)
        artifact_hash = self._single_json_review_subject(
            "idea_set.json", idea_set)
        provenance = controlled["internal_provenance"]
        engine_version = provenance.get(
            "engine_version", route["engine_version"])
        adapter_version = provenance.get(
            "adapter_version", route["adapter_version"])
        if (engine_version != route["engine_version"]
                or adapter_version != route["adapter_version"]):
            raise RuntimeMCPError(
                "WildIdea internal audit engine/adapter 身份漂移")
        provenance_hash = "sha256:" + hashlib.sha256(
            self._canonical_bytes(provenance)).hexdigest()
        payload = {
            "protocol": "runtime-wildidea-result-v1",
            "cycle_id": scope.cycle_id, "stage": scope.stage,
            "target_id": scope.target_id, "purpose": scope.purpose,
            "pack_hash": scope.pack_hash,
            "runner_call_id": scope.runner_call_id,
            "route_decision_id": route_decision_id,
            "route_receipt_hash": route["receipt_hash"],
            "generation_path": "wildidea",
            "artifact_hash": artifact_hash,
            "engine_version": engine_version,
            "adapter_version": adapter_version,
            "internal_provenance": provenance,
            "internal_provenance_hash": provenance_hash,
            "judge_runner_call_id": provenance.get(
                "judge_runner_call_id"),
            "judge_provider_receipt_hash": provenance.get(
                "judge_provider_receipt_hash"),
        }
        payload["receipt_hash"] = self._receipt_hash(payload)
        self._validate_wildidea_result_receipt(payload)
        fence = (
            commit_fence() if commit_fence is not None
            else nullcontext(lambda: None))
        with fence as assert_commit_authorized:
            with self.daemon.transaction() as conn:
                current_route = self._idea_route_in_txn(conn, scope)
                if current_route != route_row:
                    raise RuntimeMCPError(
                        "WildIdea generation_path 在 internal audit 后漂移")
                existing = self._wildidea_result_in_txn(
                    conn, scope, route_decision_id, route["receipt_hash"])
                if existing is not None:
                    raise RuntimeMCPError(
                        "WildIdea internal audit 并发重复")
                decision_id = conn.execute(
                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                    "VALUES (?,'orchestrator','runtime_wildidea_result',?)",
                    (_cycle_number(scope.cycle_id), json.dumps(
                        payload, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")))).lastrowid
                assert_commit_authorized()
        return {
            "ok": True, "decision_id": int(decision_id),
            "idea_set": idea_set,
            "artifact_hash": artifact_hash,
            "result_receipt_hash": payload["receipt_hash"],
        }

    def _wildidea_search(self, scope: RuntimeMCPScope,
                         arguments: Dict[str, Any]) -> Dict[str, Any]:
        if scope.stage != "idea":
            raise RuntimeMCPError("wildidea_search 只允许 Idea 主阶段调用")
        if set(arguments) != {"queries"}:
            raise RuntimeMCPError("wildidea_search 须且只须提供 queries")
        if self.wildidea_adapter is None:
            raise RuntimeMCPError("当前运行时未装配 WildIdea capability")
        try:
            result = self.wildidea_adapter.search_for_tool(arguments.get("queries"))
        except Exception as error:
            raise RuntimeMCPError(f"WildIdea novelty search 失败: {error}") from error
        return {"ok": True, **result}

    def _register_baseline(self, scope: RuntimeMCPScope,
                           arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Retired write path kept only to fail old direct callers loudly.

        Plan preflight is read-only.  Baseline identity is claimed exactly once
        by the Plan core gate after the committed artifact receipt is consumed.
        """
        del scope, arguments
        raise RuntimeMCPError(
            "register_baseline 已移除；请用 preflight_plan 做只读预检，"
            "baseline claim 只能在 Plan 核心 gate 短事务中提交")

    def _upsert_card(self, scope: RuntimeMCPScope,
                     arguments: Dict[str, Any]) -> Dict[str, Any]:
        goal_id, goal_ver, _active_q, _status = self._cycle_row(scope)
        ci = _cycle_number(scope.cycle_id)
        requested = arguments.get("card_type")
        ref_id = arguments.get("ref_id")
        if requested not in {"question", "baseline", "method", "failure"}:
            raise RuntimeMCPError("card_type 非法")
        if isinstance(ref_id, bool) or not isinstance(ref_id, int) or ref_id <= 0:
            raise RuntimeMCPError("ref_id 须为正整数")
        card_md = _bounded_text(
            arguments.get("card_md"), name="card_md", maximum=262144)
        storage_type = "protocol" if requested == "method" else requested
        with self.daemon.transaction() as conn:
            if requested == "question":
                row = conn.execute(
                    "SELECT goal_id,goal_ver FROM question WHERE id=?", (ref_id,)).fetchone()
                if row is None or tuple(row) != (goal_id, goal_ver):
                    raise RuntimeMCPError("question card 必须引用 current goal 的现有 question")
            elif requested == "baseline":
                if conn.execute("SELECT 1 FROM baseline WHERE id=?", (ref_id,)).fetchone() is None:
                    raise RuntimeMCPError("baseline card 引用不存在")
            elif requested == "method":
                if conn.execute("SELECT 1 FROM protocol WHERE id=?", (ref_id,)).fetchone() is None:
                    raise RuntimeMCPError("method card 必须引用现有 protocol id")
            else:
                row = conn.execute(
                    "SELECT cycle_id FROM build_target WHERE id=?", (ref_id,)).fetchone()
                if row is None or row[0] != ci:
                    raise RuntimeMCPError("failure card 必须引用当前 cycle 的 build_target")
            card_id = self._upsert_card_in_txn(
                conn, storage_type=storage_type, ref_id=ref_id,
                goal_id=goal_id, goal_ver=goal_ver, cycle_id=ci, card_md=card_md)
        return {
            "ok": True, "card_id": card_id, "card_type": requested,
            "storage_type": storage_type, "ref_id": ref_id,
            "src_hash": _card_hash(card_md),
        }

    @staticmethod
    def _upsert_card_in_txn(conn, *, storage_type: str, ref_id: int,
                            goal_id: int, goal_ver: int, cycle_id: int,
                            card_md: str) -> int:  # noqa: ANN001
        digest = _card_hash(card_md)
        conn.execute(
            "INSERT INTO card(card_type,ref_id,goal_id,goal_ver,card_md,src_hash,"
            "updated_cycle,stale) VALUES (?,?,?,?,?,?,?,0) "
            "ON CONFLICT(card_type,ref_id) DO UPDATE SET "
            "goal_id=excluded.goal_id,goal_ver=excluded.goal_ver,"
            "card_md=excluded.card_md,src_hash=excluded.src_hash,"
            "updated_cycle=excluded.updated_cycle,stale=0",
            (storage_type, ref_id, goal_id, goal_ver, card_md, digest, cycle_id))
        row = conn.execute(
            "SELECT id FROM card WHERE card_type=? AND ref_id=?", (storage_type, ref_id)
        ).fetchone()
        if row is None:
            raise RuntimeError("card upsert 后缺 durable row")
        return int(row[0])

    @staticmethod
    def _review_kind_for_stage(scope: RuntimeMCPScope, review_kind: Any) -> str:
        allowed = {
            "idea": {"idea"}, "plan": {"plan"},
            "bundle": {"bundle_code", "bundle_result"},
        }
        if review_kind not in allowed.get(scope.stage, set()):
            raise RuntimeMCPError(
                f"review_kind={review_kind!r} 不属于当前 stage={scope.stage!r}")
        return str(review_kind)

    def _configured_review_rounds(
            self, scope: RuntimeMCPScope, review_kind: str) -> int:
        key = {
            "idea": "plan_review", "plan": "plan_review",
            "bundle_code": "bundle_code_review",
            "bundle_result": "bundle_result_review",
        }[review_kind]
        retry = (((self.policy or {}).get("flow") or {}).get("retry") or {})
        rounds = retry.get(key, 0)
        if (isinstance(rounds, bool) or not isinstance(rounds, int)
                or rounds < 0 or rounds > _MAX_NATIVE_REVIEW_ROUNDS):
            raise RuntimeMCPError(
                f"runtime review 配置须为 0..{_MAX_NATIVE_REVIEW_ROUNDS} 的整数")
        return rounds

    @staticmethod
    def _native_review_focus(review_kind: str) -> Optional[Dict[str, Any]]:
        """Return owner-authored audit scope without overstating its evidence.

        Bundle code review is the independent, pre-execution inspection point
        for plan/protocol and data-boundary conformance.  It is an attestation
        over the submitted code, not telemetry proving which bytes a later
        process actually accessed.
        """
        if review_kind != "bundle_code":
            return None
        return {
            "protocol": "bundle-code-review-focus-v1",
            "required_checks": [
                "frozen_plan_conformance",
                "train_validation_test_isolation",
                "heldout_access_order",
                "train_only_preprocessing_fit",
                "outcome_leakage",
            ],
            "evidence_limit":
                "reviewer attestation; not runtime proof of heldout non-access",
        }

    @staticmethod
    def _single_json_review_subject(
            name: str, payload: Mapping[str, Any]) -> str:
        """Return the artifact hash produced for one canonical inline JSON file."""
        body = RuntimeIngestService._canonical_bytes(dict(payload))
        descriptor = {
            "files": [{
                "name": name, "kind": "json", "size_bytes": len(body),
                "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
            }],
            "md": None,
        }
        return "sha256:" + hashlib.sha256(
            RuntimeIngestService._canonical_bytes(descriptor)).hexdigest()

    def _bundle_result_owner_candidate(
            self, scope: RuntimeMCPScope, *,
            conn: Optional[sqlite3.Connection] = None,
            ) -> Dict[str, Any]:
        """Build the immutable *pre-admission* Bundle result subject.

        The orchestrator creates this candidate after eval/scientific
        classification but before publication, SQL metric registration, legal
        pool admission, or target completion.  The model cannot supply or
        replace it.
        """
        if scope.stage != "bundle" or scope.target_id is None:
            raise RuntimeMCPError(
                "bundle_result owner candidate 缺当前 Bundle target")
        try:
            target_id = int(scope.target_id)
        except (TypeError, ValueError) as error:
            raise RuntimeMCPError(
                "bundle_result owner candidate target_id 非法") from error

        ci = _cycle_number(scope.cycle_id)
        query_one = (
            self.daemon.query_one if conn is None
            else lambda sql, params=(): conn.execute(sql, params).fetchone())
        query = (
            self.daemon.query if conn is None
            else lambda sql, params=(): conn.execute(sql, params).fetchall())
        target = query_one(
            "SELECT cycle_id,target_kind,seq,status,failure_kind "
            "FROM build_target WHERE id=?", (target_id,))
        if target is None or int(target[0]) != _cycle_number(scope.cycle_id):
            raise RuntimeMCPError(
                "bundle_result owner candidate target 不属于当前 cycle")
        if target[3] not in {"running", "complete"}:
            raise RuntimeMCPError(
                "bundle_result review 只允许 pre-admission running "
                "或已完成且可回放的 target")
        rows = query(
            "SELECT d.id,d.payload_json FROM decision d "
            "WHERE d.cycle_id=? AND d.actor='orchestrator' "
            "AND d.type='bundle_result_candidate' "
            "AND json_valid(d.payload_json) "
            "AND json_extract(d.payload_json,'$.build_target_id')=? "
            "AND NOT EXISTS (SELECT 1 FROM decision s "
            " WHERE s.cycle_id=d.cycle_id AND s.actor='orchestrator' "
            " AND s.type='bundle_result_candidate_superseded' "
            " AND json_valid(s.payload_json) "
            " AND json_extract(s.payload_json,'$.candidate_decision_id')=d.id) "
            "ORDER BY d.id", (ci, target_id))
        if len(rows) != 1:
            raise RuntimeMCPError(
                "bundle_result review 要求唯一 active pre-admission candidate")
        candidate_decision_id, raw = rows[0]
        try:
            payload = _strict_json_object(
                raw, label="bundle_result candidate")
        except RuntimeMCPError:
            raise
        required = {
            "protocol", "cycle_id", "build_target_id", "evaluation_id",
            "evaluation_attempt_id", "result_subject_hash",
            "scientific_decision_hash", "execution_status",
            "validity_status", "scientific_outcome", "pool_eligibility",
            "metric_results", "eval_log", "checkpoint_hashes",
        }
        if (set(payload) != required
                or payload.get("protocol") != "bundle-result-candidate-v1"
                or payload.get("cycle_id") != ci
                or payload.get("build_target_id") != target_id
                or payload.get("execution_status") != "succeeded"
                or payload.get("validity_status") != "valid"
                or payload.get("scientific_outcome")
                not in {"supported", "refuted", "inconclusive"}
                or payload.get("pool_eligibility") != "eligible"):
            raise RuntimeMCPError(
                "bundle_result candidate 协议/科学状态/身份非法")
        attempt_id = payload.get("evaluation_attempt_id")
        evaluation_id = payload.get("evaluation_id")
        if any(
                isinstance(value, bool) or not isinstance(value, int)
                or value <= 0
                for value in (candidate_decision_id, attempt_id, evaluation_id)):
            raise RuntimeMCPError("bundle_result candidate id 非法")
        attempt = query_one(
            "SELECT evaluation_id,build_target_id,status "
            "FROM evaluation_attempt WHERE id=?", (attempt_id,))
        if (attempt is None or tuple(attempt[:2])
                != (evaluation_id, target_id)
                or attempt[2] not in {"running", "success"}):
            raise RuntimeMCPError(
                "bundle_result candidate 与 evaluation attempt 冲突")
        if target[3] == "running" and attempt[2] != "running":
            raise RuntimeMCPError(
                "pre-admission target 不得引用已 success attempt")
        return {
            "protocol": "bundle-result-review-candidate-v2",
            "candidate_decision_id": int(candidate_decision_id),
            "cycle_id": scope.cycle_id,
            "build_target_id": target_id,
            "target_kind": str(target[1]),
            "seq": int(target[2]),
            "candidate": payload,
        }

    def _assert_bundle_result_reviewable(
            self, scope: RuntimeMCPScope) -> None:
        """Require the official controller to be stopped at a clean terminal."""
        controller = self._require_bundle_controller(scope)
        try:
            status = controller.bundle_session_status(scope)
        except Exception as error:
            raise RuntimeMCPError(
                f"bundle_result owner status 不可读取: {error}") from error
        if (not isinstance(status, Mapping)
                or str(status.get("build_target_id")) != str(scope.target_id)
                or status.get("status") != "running"
                or status.get("terminal") is not False
                or status.get("worker_running") is not False
                or status.get("controller_error") not in {None, ""}
                or status.get("awaiting_result_review") is not True
                or status.get("result_candidate_decision_id") is None):
            raise RuntimeMCPError(
                "bundle_result review 要求 owner 报告 pre-admission "
                "candidate、worker 已停止且 controller 无错误")

    @staticmethod
    def _bundle_result_owner_arguments(
            candidate: Mapping[str, Any]) -> Dict[str, Any]:
        return {"files": {"bundle_result.json": dict(candidate)}}

    @staticmethod
    def _native_review_scope(
            scope: RuntimeMCPScope) -> tuple[NativeReviewLedger, int, str, str]:
        ledger = scope.native_review_ledger
        runner_call_id = scope.runner_call_id
        if ledger is None or not isinstance(ledger, NativeReviewLedger):
            raise RuntimeMCPError("当前 capability 未绑定 trusted live native-review ledger")
        if (isinstance(runner_call_id, bool)
                or not isinstance(runner_call_id, int) or runner_call_id <= 0):
            raise RuntimeMCPError("当前 capability 未绑定 trusted runner_call_id")
        try:
            parent_thread_id, parent_turn_id = ledger.parent_identity()
        except NativeReviewError as error:
            raise RuntimeMCPError(f"native reviewer parent binding 不完整: {error}") from error
        return ledger, runner_call_id, parent_thread_id, parent_turn_id

    def _materialize_review_candidate(
            self, scope: RuntimeMCPScope, arguments: Dict[str, Any], *,
            root: Path, store_key: str,
            required_submission_kind: Optional[str] = None,
            ) -> tuple[str, str, str, Dict[str, Any]]:
        """Store one bounded candidate and return subject/manifest identities."""
        if self.work_root is None:
            raise RuntimeMCPError("runtime MCP 未装配 work_root")
        md = arguments.get("md", "")
        if not isinstance(md, str) or len(md.encode("utf-8")) > 262144:
            raise RuntimeMCPError("md 须为不超过 262144 bytes 的字符串")
        files, incoming = self._collect_stage_files(
            scope, {
                "files": arguments.get("files"),
                **({"workspace_files": arguments["workspace_files"]}
                   if "workspace_files" in arguments else {}),
                **({"md": md} if md else {}),
            }, store_key=store_key)
        created_root = False
        try:
            if required_submission_kind is not None:
                actual_kind = self._validate_stage_files(scope, files)
                if actual_kind != required_submission_kind:
                    raise RuntimeMCPError(
                        "revised review candidate 不是当前 review kind 的"
                        f"最终阶段产物: {actual_kind!r}")
            if root.exists() or root.is_symlink():
                raise RuntimeMCPError("native review candidate 目录身份冲突")
            root.mkdir(parents=True, mode=0o700)
            created_root = True
            managed_names = set()
            manifest = files.get("execution_manifest.json")
            if scope.stage == "bundle" and isinstance(manifest, dict):
                code_files = manifest.get("code_files", [])
                if isinstance(code_files, list):
                    managed_names = {
                        name for name in code_files if isinstance(name, str)}
            entries: list[Dict[str, Any]] = []
            from .artifact_capability import open_artifact
            for name in sorted(files):
                value = files[name]
                is_managed = (
                    name in managed_names
                    or (hasattr(value, "path") and hasattr(value, "sha256")))
                if hasattr(value, "path") and hasattr(value, "sha256"):
                    source_path = Path(value.path)
                    expected_size = int(value.size_bytes)
                    expected_hash = str(value.sha256)
                    target = root / "managed" / Path(*name.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with open_artifact(
                            source_path, expected_hash=expected_hash,
                            expected_size=expected_size,
                            label=f"native review candidate {name}") as capability:
                        with target.open("xb") as output:
                            while True:
                                chunk = os.read(capability.fd, 1024 * 1024)
                                if not chunk:
                                    break
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        os.chmod(target, 0o600)
                        capability.verify_unchanged()
                    payload_size = expected_size
                    payload_hash = expected_hash
                else:
                    if name.endswith(".json"):
                        body = self._canonical_bytes(value)
                    elif isinstance(value, str):
                        body = value.encode("utf-8")
                    else:
                        body = self._canonical_bytes(value)
                    target = root / (
                        "managed" if is_managed else "files") / Path(*name.split("/"))
                    self._atomic_payload_file(target, body)
                    payload_size = len(body)
                    payload_hash = (
                        "sha256:" + hashlib.sha256(body).hexdigest())
                entries.append({
                    "name": name,
                    "kind": "managed" if is_managed else (
                        "json" if name.endswith(".json") else "text"),
                    "path": str(target), "size_bytes": payload_size,
                    "sha256": payload_hash,
                })
            md_entry = None
            if md:
                body = md.encode("utf-8")
                path = root / "stage.md"
                self._atomic_payload_file(path, body)
                md_entry = {
                    "path": str(path), "size_bytes": len(body),
                    "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                }
            descriptor = {
                "files": [{key: item[key] for key in (
                    "name", "kind", "size_bytes", "sha256")}
                          for item in entries],
                "md": (None if md_entry is None else {
                    "size_bytes": md_entry["size_bytes"],
                    "sha256": md_entry["sha256"],
                }),
            }
            subject_hash = "sha256:" + hashlib.sha256(
                self._canonical_bytes(descriptor)).hexdigest()
            candidate_manifest = {
                "protocol": "native-review-candidate-v1",
                "artifact_hash": subject_hash,
                "files": entries,
                "md": md_entry,
            }
            manifest_path = root / "candidate-manifest.json"
            manifest_bytes = self._canonical_bytes(candidate_manifest)
            self._atomic_payload_file(manifest_path, manifest_bytes)
            manifest_hash = (
                "sha256:" + hashlib.sha256(manifest_bytes).hexdigest())
            return subject_hash, str(manifest_path), manifest_hash, candidate_manifest
        except BaseException:
            if created_root:
                shutil.rmtree(root, ignore_errors=True)
            raise
        finally:
            if incoming is not None:
                shutil.rmtree(incoming, ignore_errors=True)

    @staticmethod
    def _receipt_hash(payload: Mapping[str, Any]) -> str:
        core = dict(payload)
        core.pop("receipt_hash", None)
        return "sha256:" + hashlib.sha256(
            RuntimeIngestService._canonical_bytes(core)).hexdigest()

    @staticmethod
    def _validate_native_receipt(payload: Dict[str, Any]) -> None:
        required = {
            "protocol", "review_request_id", "cycle_id", "stage",
            "target_id", "purpose", "review_kind", "round_no",
            "configured_rounds", "reviewed_subject_hash",
            "resulting_subject_hash", "prior_receipt_hash", "runner_call_id",
            "parent_thread_id", "parent_turn_id", "child_call_id",
            "child_thread_id", "child_turn_id", "verdict",
            "review_input_item_id", "review_input_brief_hash",
            "review_input_candidate_manifest_hash",
            "findings_ref", "findings_hash", "dispositions_ref",
            "disposition_hash", "revised_candidate_manifest_ref",
            "revised_candidate_manifest_hash", "receipt_hash",
        }
        if set(payload) != required:
            raise RuntimeMCPError("native-review-receipt-v1 字段闭包非法")
        if (payload.get("protocol") != "native-review-receipt-v1"
                or payload.get("verdict") not in {"pass", "fail"}):
            raise RuntimeMCPError("native review receipt 协议/verdict 非法")
        for field in (
                "reviewed_subject_hash", "resulting_subject_hash",
                "findings_hash", "disposition_hash",
                "revised_candidate_manifest_hash", "receipt_hash",
                "review_input_brief_hash",
                "review_input_candidate_manifest_hash"):
            if _SHA256.fullmatch(str(payload.get(field) or "")) is None:
                raise RuntimeMCPError(f"native review receipt {field} 非法")
        if _SAFE_KEY.fullmatch(
                str(payload.get("review_input_item_id") or "")) is None:
            raise RuntimeMCPError(
                "native review receipt review_input_item_id 非法")
        prior = payload.get("prior_receipt_hash")
        if prior is not None and _SHA256.fullmatch(str(prior)) is None:
            raise RuntimeMCPError("native review receipt prior_receipt_hash 非法")
        for field in ("round_no", "configured_rounds", "runner_call_id"):
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RuntimeMCPError(f"native review receipt {field} 非法")
        if payload["round_no"] > payload["configured_rounds"]:
            raise RuntimeMCPError("native review receipt round 超出配置")
        if RuntimeIngestService._receipt_hash(payload) != payload["receipt_hash"]:
            raise RuntimeMCPError("native review receipt hash 不可复验")

    @staticmethod
    def _durable_native_review_child_proof(
            payload: Dict[str, Any],
            execution: NativeReviewExecutionEvidence,
            ) -> Dict[str, Any]:
        """Bind a durable review receipt to one owner-replayed child.

        This is deliberately not an MCP tool.  The shared verifier supplies
        ``execution`` either from the owner-persisted parsed live prefix while
        the runner is still running, or from the terminal guardian capture
        referenced by exact provider accounting after success.  The resident
        main agent cannot supply either evidence object.
        """
        RuntimeIngestService._validate_native_receipt(payload)
        if not isinstance(execution, NativeReviewExecutionEvidence):
            raise RuntimeMCPError(
                "native review durable execution evidence 非法")
        if (
                execution.runner_call_id != payload["runner_call_id"]
                or execution.cycle_id != payload["cycle_id"]
                or execution.stage != payload["stage"]
                or execution.purpose != payload["purpose"]):
            raise RuntimeMCPError(
                "native review receipt 与 guardian runner scope 不一致")

        matches = [
            evidence for evidence in execution.children
            if (
                evidence.review_input is not None
                and evidence.review_input.review_request_id
                == payload["review_request_id"])
        ]
        if len(matches) != 1:
            raise RuntimeMCPError(
                "owner replay 未证明唯一 matching completed reviewer child")
        evidence = matches[0]
        delivered = evidence.review_input
        assert delivered is not None
        if (
                evidence.parent_thread_id != payload["parent_thread_id"]
                or evidence.parent_turn_id != payload["parent_turn_id"]
                or evidence.call_id != payload["child_call_id"]
                or evidence.child_thread_id != payload["child_thread_id"]
                or evidence.child_turn_id != payload["child_turn_id"]
                or delivered.item_id != payload["review_input_item_id"]
                or delivered.reviewer_brief_hash
                != payload["review_input_brief_hash"]
                or delivered.candidate_manifest_hash
                != payload["review_input_candidate_manifest_hash"]):
            raise RuntimeMCPError(
                "native review receipt 与 owner child/input 身份不一致")
        result = RuntimeIngestService._parse_native_child_result(evidence)
        if (result["review_request_id"] != payload["review_request_id"]
                or result["verdict"] != payload["verdict"]):
            raise RuntimeMCPError(
                "native review receipt 与 child terminal result 不一致")
        findings_hash = "sha256:" + hashlib.sha256(
            RuntimeIngestService._canonical_bytes(
                result["findings"])).hexdigest()
        if findings_hash != payload["findings_hash"]:
            raise RuntimeMCPError(
                "native review receipt findings hash 与 child terminal 不一致")

        proof = {
            "protocol": "native-review-child-event-proof-v1",
            "runner_call_id": payload["runner_call_id"],
            "cycle_id": payload["cycle_id"],
            "stage": payload["stage"],
            "target_id": payload["target_id"],
            "purpose": payload["purpose"],
            "execution_receipt_ref":
                execution.execution_receipt_ref,
            "execution_operation_id":
                execution.execution_operation_id,
            "capture_stdout_sha256":
                execution.capture_stdout_sha256,
            "parent_thread_id": evidence.parent_thread_id,
            "parent_turn_id": evidence.parent_turn_id,
            "child_call_id": evidence.call_id,
            "child_thread_id": evidence.child_thread_id,
            "child_turn_id": evidence.child_turn_id,
            "review_request_id": payload["review_request_id"],
            "review_input_item_id": delivered.item_id,
            "review_input_brief_hash":
                delivered.reviewer_brief_hash,
            "review_input_candidate_manifest_hash":
                delivered.candidate_manifest_hash,
            "child_final_hash": "sha256:" + hashlib.sha256(
                evidence.final_bytes).hexdigest(),
            "verdict": result["verdict"],
            "findings_hash": findings_hash,
            "round_no": payload["round_no"],
            "configured_rounds": payload["configured_rounds"],
            "reviewed_subject_hash":
                payload["reviewed_subject_hash"],
            "resulting_subject_hash":
                payload["resulting_subject_hash"],
            "prior_receipt_hash": payload["prior_receipt_hash"],
            "review_receipt_hash": payload["receipt_hash"],
        }
        proof["proof_hash"] = "sha256:" + hashlib.sha256(
            RuntimeIngestService._canonical_bytes(proof)).hexdigest()
        return proof

    def _native_review_chain_in_txn(
            self, conn: sqlite3.Connection, scope: RuntimeMCPScope,
            review_kind: str, *, runner_call_id: int,
            parent_thread_id: str, parent_turn_id: str,
            ) -> list[tuple[int, Dict[str, Any]]]:
        rows = conn.execute(
            "SELECT id,payload_json FROM decision "
            "WHERE cycle_id=? AND type='runtime_review' ORDER BY id",
            (_cycle_number(scope.cycle_id),)).fetchall()
        selected = []
        for decision_id, raw in rows:
            payload = _strict_json_object(raw, label="runtime_review decision")
            if payload.get("protocol") != "native-review-receipt-v1":
                # Historical caller-authored reviews remain readable history,
                # but are never authority for the native gate.
                continue
            self._validate_native_receipt(payload)
            if (
                    payload["cycle_id"] == scope.cycle_id
                    and payload["stage"] == scope.stage
                    and payload["target_id"] == scope.target_id
                    and payload["purpose"] == scope.purpose
                    and payload["review_kind"] == review_kind
                    and payload["runner_call_id"] == runner_call_id
                    and payload["parent_thread_id"] == parent_thread_id
                    and payload["parent_turn_id"] == parent_turn_id):
                selected.append((int(decision_id), payload))
        chains: list[list[tuple[int, Dict[str, Any]]]] = []
        current: list[tuple[int, Dict[str, Any]]] = []
        for item in selected:
            payload = item[1]
            starts_chain = (
                payload["round_no"] == 1
                and payload["prior_receipt_hash"] is None)
            if starts_chain:
                if current:
                    configured = current[0][1]["configured_rounds"]
                    if len(current) != configured:
                        raise RuntimeMCPError(
                            "native review 新候选开始前旧 chain 未完成")
                    chains.append(current)
                current = [item]
                continue
            if not current:
                raise RuntimeMCPError("native review chain 缺 round 1")
            prior = current[-1][1]
            if (payload["configured_rounds"]
                    != current[0][1]["configured_rounds"]
                    or payload["round_no"] != len(current) + 1
                    or payload["prior_receipt_hash"] != prior["receipt_hash"]
                    or payload["reviewed_subject_hash"]
                    != prior["resulting_subject_hash"]):
                raise RuntimeMCPError(
                    "native review round/subject/prior receipt lineage 非法")
            current.append(item)
        if current:
            chains.append(current)
        if len(chains) > 1 and review_kind not in {
                "bundle_code", "bundle_result"}:
            raise RuntimeMCPError(
                f"{review_kind} 不允许同一 stage turn 启动第二条 review chain")
        return chains[-1] if chains else []

    @staticmethod
    def _validate_review_request(payload: Dict[str, Any]) -> None:
        required = {
            "protocol", "review_request_id", "cycle_id", "stage",
            "target_id", "purpose", "review_kind", "round_no",
            "configured_rounds", "reviewed_subject_hash",
            "prior_receipt_hash", "runner_call_id", "parent_thread_id",
            "parent_turn_id", "candidate_manifest_ref",
            "candidate_manifest_hash", "reviewer_brief_ref",
            "reviewer_brief_hash",
        }
        if set(payload) != required or payload.get(
                "protocol") != "native-review-request-v1":
            raise RuntimeMCPError("native review request 字段闭包/协议非法")
        for field in (
                "reviewed_subject_hash", "candidate_manifest_hash",
                "reviewer_brief_hash"):
            if _SHA256.fullmatch(str(payload.get(field) or "")) is None:
                raise RuntimeMCPError(f"native review request {field} 非法")
        prior = payload.get("prior_receipt_hash")
        if prior is not None and _SHA256.fullmatch(str(prior)) is None:
            raise RuntimeMCPError("native review request prior receipt 非法")
        for field in ("round_no", "configured_rounds", "runner_call_id"):
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RuntimeMCPError(f"native review request {field} 非法")
        if payload["round_no"] > payload["configured_rounds"]:
            raise RuntimeMCPError("native review request round 超出配置")

    def _review_request_in_txn(
            self, conn: sqlite3.Connection, request_id: str, *,
            cycle_id: str,
            ) -> tuple[int, Dict[str, Any]]:
        rows = conn.execute(
            "SELECT id,payload_json FROM decision "
            "WHERE cycle_id=? AND type='runtime_review_request' ORDER BY id",
            (_cycle_number(cycle_id),)).fetchall()
        matches = []
        for decision_id, raw in rows:
            payload = _strict_json_object(
                raw, label="runtime_review_request decision")
            self._validate_review_request(payload)
            if payload["review_request_id"] == request_id:
                matches.append((int(decision_id), payload))
        if len(matches) != 1:
            raise RuntimeMCPError("review_request_id 不存在或身份冲突")
        return matches[0]

    @staticmethod
    def _review_request_matches_scope(
            request: Mapping[str, Any], scope: RuntimeMCPScope, *,
            runner_call_id: int, parent_thread_id: str,
            parent_turn_id: str) -> bool:
        return (
            request.get("cycle_id") == scope.cycle_id
            and request.get("stage") == scope.stage
            and request.get("target_id") == scope.target_id
            and request.get("purpose") == scope.purpose
            and request.get("runner_call_id") == runner_call_id
            and request.get("parent_thread_id") == parent_thread_id
            and request.get("parent_turn_id") == parent_turn_id)

    def _verified_review_input(
            self, request: Mapping[str, Any]) -> Dict[str, Any]:
        """Re-read one owner-indexed review input and verify every file identity."""
        if self.work_root is None:
            raise RuntimeMCPError("runtime MCP 未装配 work_root")
        try:
            from .artifact_capability import (
                ArtifactCapabilityError,
                open_artifact,
                read_artifact_bytes,
            )

            storage_root = (
                self.work_root / "runtime" / "native-reviews").resolve(strict=True)

            def confined(raw: Any, *, label: str) -> Path:
                if not isinstance(raw, str) or not raw:
                    raise RuntimeMCPError(f"{label} path 非法")
                path = Path(raw).resolve(strict=True)
                if os.path.commonpath(
                        (str(path), str(storage_root))) != str(storage_root):
                    raise RuntimeMCPError(f"{label} 逃逸 native-review 根")
                return path

            brief_path = confined(
                request.get("reviewer_brief_ref"), label="reviewer brief")
            brief_bytes = read_artifact_bytes(
                brief_path,
                expected_hash=str(request.get("reviewer_brief_hash")),
                max_bytes=384 * 1024,
                label="native reviewer brief")
            brief = _strict_json_object(
                brief_bytes, label="native reviewer brief")

            manifest_path = confined(
                request.get("candidate_manifest_ref"),
                label="review candidate manifest")
            manifest_bytes = read_artifact_bytes(
                manifest_path,
                expected_hash=str(request.get("candidate_manifest_hash")),
                max_bytes=384 * 1024,
                label="native review candidate manifest")
            manifest = _strict_json_object(
                manifest_bytes, label="native review candidate manifest")
            if (manifest_path.name != "candidate-manifest.json"
                    or manifest_path.parent.name != "candidate"):
                raise RuntimeMCPError(
                    "native review candidate manifest 位置非法")
            candidate_root = manifest_path.parent
            if (manifest.get("protocol") != "native-review-candidate-v1"
                    or manifest.get("artifact_hash")
                    != request.get("reviewed_subject_hash")
                    or not isinstance(manifest.get("files"), list)
                    or len(manifest["files"]) > 1024
                    or manifest.get("md") is not None
                    and not isinstance(manifest.get("md"), dict)):
                raise RuntimeMCPError(
                    "native review candidate manifest 协议/身份非法")

            descriptor_files = []
            seen_names = set()
            for entry in manifest["files"]:
                if (not isinstance(entry, dict)
                        or set(entry) != {
                            "name", "kind", "path", "size_bytes", "sha256"}):
                    raise RuntimeMCPError(
                        "native review candidate entry 字段闭包非法")
                name = self._safe_artifact_name(entry.get("name"))
                if name in seen_names:
                    raise RuntimeMCPError(
                        "native review candidate entry 重复")
                seen_names.add(name)
                kind = entry.get("kind")
                size = entry.get("size_bytes")
                digest = entry.get("sha256")
                if (kind not in {"managed", "json", "text"}
                        or isinstance(size, bool) or not isinstance(size, int)
                        or size < 0
                        or _SHA256.fullmatch(str(digest or "")) is None):
                    raise RuntimeMCPError(
                        "native review candidate entry 身份非法")
                path = confined(
                    entry.get("path"),
                    label=f"review candidate {name}")
                expected_path = (
                    candidate_root
                    / ("managed" if kind == "managed" else "files")
                    / Path(*name.split("/"))).resolve(strict=True)
                if path != expected_path:
                    raise RuntimeMCPError(
                        f"native review candidate {name} 路径绑定非法")
                with open_artifact(
                        path, expected_hash=digest, expected_size=size,
                        label=f"native review candidate {name}"):
                    pass
                descriptor_files.append({
                    "name": name, "kind": kind,
                    "size_bytes": size, "sha256": digest,
                })

            md_entry = manifest.get("md")
            descriptor_md = None
            if md_entry is not None:
                if (set(md_entry) != {"path", "size_bytes", "sha256"}
                        or isinstance(md_entry.get("size_bytes"), bool)
                        or not isinstance(md_entry.get("size_bytes"), int)
                        or md_entry["size_bytes"] < 0
                        or _SHA256.fullmatch(
                            str(md_entry.get("sha256") or "")) is None):
                    raise RuntimeMCPError(
                        "native review candidate md 身份非法")
                md_path = confined(
                    md_entry.get("path"), label="review candidate md")
                if md_path != (candidate_root / "stage.md").resolve(strict=True):
                    raise RuntimeMCPError(
                        "native review candidate md 路径绑定非法")
                with open_artifact(
                        md_path, expected_hash=md_entry["sha256"],
                        expected_size=md_entry["size_bytes"],
                        label="native review candidate md"):
                    pass
                descriptor_md = {
                    "size_bytes": md_entry["size_bytes"],
                    "sha256": md_entry["sha256"],
                }
            descriptor = {
                "files": descriptor_files,
                "md": descriptor_md,
            }
            descriptor_hash = "sha256:" + hashlib.sha256(
                self._canonical_bytes(descriptor)).hexdigest()
            if descriptor_hash != manifest["artifact_hash"]:
                raise RuntimeMCPError(
                    "native review candidate artifact_hash 不可复验")

            identity_fields = {
                "protocol": "native-review-brief-v1",
                "review_request_id": request.get("review_request_id"),
                "cycle_id": request.get("cycle_id"),
                "stage": request.get("stage"),
                "target_id": request.get("target_id"),
                "purpose": request.get("purpose"),
                "review_kind": request.get("review_kind"),
                "round_no": request.get("round_no"),
                "configured_rounds": request.get("configured_rounds"),
                "reviewed_subject_hash": request.get("reviewed_subject_hash"),
            }
            if (any(brief.get(key) != value
                    for key, value in identity_fields.items())
                    or brief.get("candidate_manifest") != manifest
                    or brief.get("review_focus")
                    != self._native_review_focus(request["review_kind"])
                    or brief.get("required_result_protocol")
                    != "native-review-result-v1"
                    or not isinstance(
                        brief.get("required_result_fields"), dict)):
                raise RuntimeMCPError(
                    "native reviewer brief 与持久 request/candidate 不一致")
            response = {
                "ok": True,
                "protocol": "native-review-input-v1",
                "review_request_id": request["review_request_id"],
                "reviewer_brief_hash": request["reviewer_brief_hash"],
                "candidate_manifest_hash": request["candidate_manifest_hash"],
                "reviewer_brief": brief,
            }
            if len(self._canonical_bytes(response)) > 448 * 1024:
                raise RuntimeMCPError("native reviewer input 超过事件传输上限")
            return response
        except RuntimeMCPError:
            raise
        except (ArtifactCapabilityError, OSError, ValueError) as error:
            raise RuntimeMCPError(
                f"native reviewer input 文件身份复验失败: {error}") from error

    def _read_review_input(
            self, scope: RuntimeMCPScope,
            arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Deliver canonical owner input; the live ledger proves which child read it."""
        self._cycle_row(scope)
        if set(arguments) != {"review_request_id"}:
            raise RuntimeMCPError(
                "read_review_input 须且只须提供 review_request_id")
        request_id = arguments.get("review_request_id")
        if (not isinstance(request_id, str)
                or re.fullmatch(
                    r"nrr-[A-Za-z0-9_-]{16,128}", request_id) is None):
            raise RuntimeMCPError("review_request_id 非法")
        _ledger, runner_call_id, parent_thread_id, parent_turn_id = (
            self._native_review_scope(scope))
        with self.daemon.transaction() as conn:
            _decision_id, request = self._review_request_in_txn(
                conn, request_id, cycle_id=scope.cycle_id)
        if not self._review_request_matches_scope(
                request, scope, runner_call_id=runner_call_id,
                parent_thread_id=parent_thread_id,
                parent_turn_id=parent_turn_id):
            raise RuntimeMCPError(
                "native review request binding 与当前 capability 不一致")
        return self._verified_review_input(request)

    def _prepare_review(
            self, scope: RuntimeMCPScope, arguments: Dict[str, Any], *,
            commit_fence: Optional[
                Callable[[], ContextManager[Callable[[], None]]]] = None,
            ) -> Dict[str, Any]:
        self._cycle_row(scope)
        if self.work_root is None:
            raise RuntimeMCPError("runtime MCP 未装配 work_root，不能准备 review")
        unknown = set(arguments) - {
            "review_kind", "files", "workspace_files", "md"}
        if unknown:
            raise RuntimeMCPError(f"prepare_review 含未知参数: {sorted(unknown)}")
        review_kind = self._review_kind_for_stage(
            scope, arguments.get("review_kind"))
        rounds = self._configured_review_rounds(scope, review_kind)
        if rounds == 0:
            raise RuntimeMCPError("当前 review 配置为 0，无需 prepare_review")
        if review_kind == "idea" and self.wildidea_adapter is not None:
            with self.daemon.transaction() as conn:
                route_row = self._idea_route_in_txn(conn, scope)
            if route_row is None:
                raise RuntimeMCPError(
                    "Idea native review 前必须先用 wildidea_expand "
                    "绑定 generation_path")
            route = route_row[1]
            if route["generation_path"] == "wildidea":
                raise RuntimeMCPError(
                    "generation_path=wildidea 使用 WildIdea 内部 audit，"
                    "不得启动 Idea native review")
            candidate = (arguments.get("files") or {}).get("idea_set.json")
            if isinstance(candidate, Mapping):
                self._assert_idea_matches_route(candidate, route)
        materialize_arguments = arguments
        if review_kind == "bundle_result":
            if (set(arguments) - {"review_kind", "files"}
                    or arguments.get("files", {}) != {}):
                raise RuntimeMCPError(
                    "bundle_result candidate 由 owner 生成；"
                    "caller 不得注入 files/md/workspace_files")
            self._assert_bundle_result_reviewable(scope)
            owner_candidate = self._bundle_result_owner_candidate(scope)
            materialize_arguments = self._bundle_result_owner_arguments(
                owner_candidate)
        elif "files" not in arguments:
            raise RuntimeMCPError(
                f"{review_kind} prepare_review 必须提供当前 candidate files")
        ledger, runner_call_id, parent_thread_id, parent_turn_id = (
            self._native_review_scope(scope))
        del ledger  # identity was read from the live trusted capability
        request_id = "nrr-" + secrets.token_urlsafe(24)
        ci = _cycle_number(scope.cycle_id)
        target_key = f"t{scope.target_id}" if scope.target_id is not None else "stage"
        root = (self.work_root / "runtime" / "native-reviews" /
                f"c{ci}" / scope.stage / target_key / request_id)
        subject_hash, manifest_ref, manifest_hash, candidate_manifest = (
            self._materialize_review_candidate(
                scope, materialize_arguments, root=root / "candidate",
                store_key=request_id))
        fence = (
            commit_fence() if commit_fence is not None
            else nullcontext(lambda: None))
        try:
            with fence as assert_commit_authorized:
                with self.daemon.transaction() as conn:
                    if review_kind == "bundle_result":
                        current_owner = self._bundle_result_owner_candidate(
                            scope, conn=conn)
                        current_owner_hash = self._single_json_review_subject(
                            "bundle_result.json", current_owner)
                        if current_owner_hash != subject_hash:
                            raise RuntimeMCPError(
                                "bundle_result owner subject 在 request "
                                "提交前发生漂移，请重新 prepare_review")
                    chain = self._native_review_chain_in_txn(
                        conn, scope, review_kind,
                        runner_call_id=runner_call_id,
                        parent_thread_id=parent_thread_id,
                        parent_turn_id=parent_turn_id)
                    if len(chain) == rounds:
                        previous_subject = chain[-1][1][
                            "resulting_subject_hash"]
                        if (review_kind in {"bundle_code", "bundle_result"}
                                and subject_hash != previous_subject):
                            # A repaired implementation or rerun result is a
                            # new candidate and therefore gets its own exact-N
                            # clean-child chain in the same resident Bundle
                            # main turn.
                            chain = []
                        else:
                            raise RuntimeMCPError(
                                f"native review 已完成精确 {rounds} 轮")
                    round_no = len(chain) + 1
                    if round_no > rounds:
                        raise RuntimeMCPError(
                            f"native review 已完成精确 {rounds} 轮")
                    prior_receipt_hash = (
                        chain[-1][1]["receipt_hash"] if chain else None)
                    current_subject = (
                        chain[-1][1]["resulting_subject_hash"] if chain else None)
                    if current_subject is not None and subject_hash != current_subject:
                        raise RuntimeMCPError(
                            "candidate hash 不等于 current review head；"
                            "下一轮必须评审上一轮 resulting subject")
                    brief = {
                        "protocol": "native-review-brief-v1",
                        "review_request_id": request_id,
                        "cycle_id": scope.cycle_id,
                        "stage": scope.stage, "target_id": scope.target_id,
                        "purpose": scope.purpose, "review_kind": review_kind,
                        "round_no": round_no,
                        "configured_rounds": rounds,
                        "reviewed_subject_hash": subject_hash,
                        "candidate_manifest": candidate_manifest,
                        "review_focus":
                            self._native_review_focus(review_kind),
                        "required_result_protocol": "native-review-result-v1",
                        "required_result_fields": {
                            "protocol": "native-review-result-v1",
                            "review_request_id": request_id,
                            "verdict": "pass|fail",
                            "summary_md": "bounded string",
                            "findings": [{
                                "finding_id": "stable ASCII id",
                                "issue": "non-empty string",
                                "rationale": "non-empty string",
                                "fix_hint": "non-empty string",
                            }],
                        },
                    }
                    brief_path = root / "reviewer-brief.json"
                    brief_bytes = self._canonical_bytes(brief)
                    self._atomic_payload_file(brief_path, brief_bytes)
                    brief_hash = (
                        "sha256:" + hashlib.sha256(brief_bytes).hexdigest())
                    payload = {
                        "protocol": "native-review-request-v1",
                        "review_request_id": request_id,
                        "cycle_id": scope.cycle_id, "stage": scope.stage,
                        "target_id": scope.target_id, "purpose": scope.purpose,
                        "review_kind": review_kind, "round_no": round_no,
                        "configured_rounds": rounds,
                        "reviewed_subject_hash": subject_hash,
                        "prior_receipt_hash": prior_receipt_hash,
                        "runner_call_id": runner_call_id,
                        "parent_thread_id": parent_thread_id,
                        "parent_turn_id": parent_turn_id,
                        "candidate_manifest_ref": manifest_ref,
                        "candidate_manifest_hash": manifest_hash,
                        "reviewer_brief_ref": str(brief_path),
                        "reviewer_brief_hash": brief_hash,
                    }
                    decision_id = conn.execute(
                        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                        "VALUES (?,'agent','runtime_review_request',?)",
                        (ci, json.dumps(
                            payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")))).lastrowid
                    assert_commit_authorized()
            return {
                "ok": True, "decision_id": int(decision_id),
                "review_request_id": request_id, "round_no": round_no,
                "configured_rounds": rounds,
                "reviewed_subject_hash": subject_hash,
                "reviewer_brief_ref": str(brief_path),
                "reviewer_brief_hash": brief_hash,
                "candidate_manifest_hash": manifest_hash,
                "reviewer_instruction": {
                    "spawn_fork_turns": "none",
                    "child_tool": "read_review_input",
                    "child_tool_arguments": {
                        "review_request_id": request_id,
                    },
                },
            }
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise

    @staticmethod
    def _parse_native_child_result(
            evidence: NativeReviewEvidence) -> Dict[str, Any]:
        if len(evidence.final_bytes) > 262144:
            raise RuntimeMCPError("native reviewer result 超过 262144 bytes")
        result = _strict_json_object(
            evidence.final_bytes, label="native reviewer terminal result")
        required = {
            "protocol", "review_request_id", "verdict",
            "summary_md", "findings"}
        if set(result) != required or result.get(
                "protocol") != "native-review-result-v1":
            raise RuntimeMCPError("native reviewer terminal result 协议/字段闭包非法")
        if result.get("verdict") not in {"pass", "fail"}:
            raise RuntimeMCPError("native reviewer verdict 非法")
        _bounded_text(
            result.get("summary_md"), name="reviewer summary_md",
            maximum=65536, allow_empty=True)
        findings = result.get("findings")
        if not isinstance(findings, list) or len(findings) > 64:
            raise RuntimeMCPError("native reviewer findings 须为最多 64 项")
        seen = set()
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {
                    "finding_id", "issue", "rationale", "fix_hint"}:
                raise RuntimeMCPError("native reviewer finding 字段闭包非法")
            finding_id = finding.get("finding_id")
            if (_SAFE_KEY.fullmatch(str(finding_id or "")) is None
                    or finding_id in seen):
                raise RuntimeMCPError("native reviewer finding_id 非法或重复")
            seen.add(finding_id)
            for field in ("issue", "rationale", "fix_hint"):
                _bounded_text(
                    finding.get(field), name=f"finding.{field}", maximum=4096)
        return result

    @staticmethod
    def _verify_native_receipt_child_binding(
            ledger: NativeReviewLedger, receipt: Mapping[str, Any],
            ) -> None:
        try:
            evidence = ledger.verify_completed_child_claim(
                str(receipt["child_thread_id"]),
                claim_id=str(receipt["review_request_id"]))
        except (KeyError, NativeReviewError) as error:
            raise RuntimeMCPError(
                f"native review request↔child 唯一绑定失效: {error}") from error
        if (
                evidence.parent_thread_id != receipt.get("parent_thread_id")
                or evidence.parent_turn_id != receipt.get("parent_turn_id")
                or evidence.call_id != receipt.get("child_call_id")
                or evidence.child_thread_id != receipt.get("child_thread_id")
                or evidence.child_turn_id != receipt.get("child_turn_id")):
            raise RuntimeMCPError(
                "native review request↔child 唯一绑定身份漂移")
        delivered = evidence.review_input
        if (
                delivered is None
                or delivered.review_request_id
                != receipt.get("review_request_id")
                or delivered.item_id
                != receipt.get("review_input_item_id")
                or delivered.reviewer_brief_hash
                != receipt.get("review_input_brief_hash")
                or delivered.candidate_manifest_hash
                != receipt.get("review_input_candidate_manifest_hash")):
            raise RuntimeMCPError(
                "native reviewer owner input delivery 身份漂移")

    @staticmethod
    def _normalize_dispositions(
            raw: Any, finding_ids: set[str]) -> list[Dict[str, str]]:
        if not isinstance(raw, list) or len(raw) > 64:
            raise RuntimeMCPError("dispositions 须为最多 64 项")
        normalized = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict) or set(item) != {
                    "finding_id", "decision", "rationale"}:
                raise RuntimeMCPError("disposition 字段闭包非法")
            finding_id = item.get("finding_id")
            if finding_id in seen or finding_id not in finding_ids:
                raise RuntimeMCPError("disposition finding_id 缺失、重复或非权威 finding")
            if item.get("decision") not in {"accept", "reject"}:
                raise RuntimeMCPError("disposition decision 非法")
            rationale = _bounded_text(
                item.get("rationale"), name="disposition rationale",
                maximum=4096)
            seen.add(finding_id)
            normalized.append({
                "finding_id": finding_id, "decision": item["decision"],
                "rationale": rationale,
            })
        if seen != finding_ids:
            raise RuntimeMCPError(
                "disposition 不完整；每个 authoritative finding 必须恰有一项")
        return sorted(normalized, key=lambda item: item["finding_id"])

    def _record_review(
            self, scope: RuntimeMCPScope, arguments: Dict[str, Any], *,
            commit_fence: Optional[
                Callable[[], ContextManager[Callable[[], None]]]] = None,
            ) -> Dict[str, Any]:
        _goal_id, _goal_ver, active_q, _status = self._cycle_row(scope)
        if self.work_root is None:
            raise RuntimeMCPError("runtime MCP 未装配 work_root，不能记录 review")
        unknown = set(arguments) - {
            "review_request_id", "dispositions",
            "files", "workspace_files", "md"}
        if unknown or {"review_request_id", "dispositions"} - set(arguments):
            raise RuntimeMCPError(
                "legacy caller-authored review 不再接受；"
                "请先 prepare_review，再提交 request_id、完整 dispositions；"
                "可修订的 review kind 还须提交 revised candidate")
        request_id = arguments.get("review_request_id")
        if (not isinstance(request_id, str)
                or re.fullmatch(r"nrr-[A-Za-z0-9_-]{16,128}", request_id) is None):
            raise RuntimeMCPError("review_request_id 非法")
        ledger, runner_call_id, parent_thread_id, parent_turn_id = (
            self._native_review_scope(scope))
        ci = _cycle_number(scope.cycle_id)
        with self.daemon.transaction() as conn:
            _request_decision_id, request = self._review_request_in_txn(
                conn, request_id, cycle_id=scope.cycle_id)
            existing_receipt = conn.execute(
                "SELECT id FROM decision WHERE cycle_id=? "
                "AND type='runtime_review' AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.protocol')="
                "'native-review-receipt-v1' "
                "AND json_extract(payload_json,'$.review_request_id')=?",
                (ci, request_id)).fetchall()
            if existing_receipt:
                raise RuntimeMCPError(
                    "native review request 已完成，拒绝重复 round")
        if not self._review_request_matches_scope(
                request, scope, runner_call_id=runner_call_id,
                parent_thread_id=parent_thread_id,
                parent_turn_id=parent_turn_id):
            raise RuntimeMCPError("native review request binding 与当前 capability 不一致")
        materialize_arguments = arguments
        if request["review_kind"] == "bundle_result":
            if (set(arguments) - {"review_request_id", "dispositions", "files"}
                    or arguments.get("files", {}) != {}):
                raise RuntimeMCPError(
                    "bundle_result revised candidate 由 owner 生成；"
                    "caller 不得注入 files/md/workspace_files")
            self._assert_bundle_result_reviewable(scope)
            owner_candidate = self._bundle_result_owner_candidate(scope)
            materialize_arguments = self._bundle_result_owner_arguments(
                owner_candidate)
        elif "files" not in arguments:
            raise RuntimeMCPError(
                f"{request['review_kind']} record_review 必须提供 revised candidate files")

        matches = []
        for evidence in ledger.completed_children():
            try:
                result = self._parse_native_child_result(evidence)
            except RuntimeMCPError:
                continue
            if result["review_request_id"] == request_id:
                matches.append((evidence, result))
        if len(matches) != 1:
            raise RuntimeMCPError(
                "live ledger 未找到唯一匹配 request_id 的 completed native child")
        evidence, result = matches[0]
        delivered = evidence.review_input
        if (
                delivered is None
                or delivered.review_request_id != request_id
                or delivered.reviewer_brief_hash
                != request["reviewer_brief_hash"]
                or delivered.candidate_manifest_hash
                != request["candidate_manifest_hash"]):
            raise RuntimeMCPError(
                "匹配 reviewer child 未通过 owner read_review_input "
                "接收当前 request 的权威输入")
        # Recheck the durable owner input before creating a revised candidate
        # or consuming the one-shot child claim. A failed check is retryable
        # with the same request after the file identity is restored.
        self._verified_review_input(request)
        findings = result["findings"]
        dispositions = self._normalize_dispositions(
            arguments.get("dispositions"),
            {item["finding_id"] for item in findings})

        target_key = f"t{scope.target_id}" if scope.target_id is not None else "stage"
        root = (self.work_root / "runtime" / "native-reviews" /
                f"c{ci}" / scope.stage / target_key / request_id)
        revised_root = root / "revised"
        resulting_hash, revised_manifest_ref, revised_manifest_hash, _manifest = (
            self._materialize_review_candidate(
                scope, materialize_arguments, root=revised_root,
                store_key=request_id + "-revised",
                required_submission_kind={
                    "idea": "idea",
                    "plan": "plan",
                    "bundle_code": "bundle",
                }.get(request["review_kind"])))
        findings_path = root / "reviewer-result.json"
        dispositions_path = root / "dispositions.json"
        findings_bytes = self._canonical_bytes(findings)
        dispositions_bytes = self._canonical_bytes(dispositions)
        self._atomic_payload_file(findings_path, findings_bytes)
        self._atomic_payload_file(dispositions_path, dispositions_bytes)
        findings_hash = "sha256:" + hashlib.sha256(
            findings_bytes).hexdigest()
        disposition_hash = "sha256:" + hashlib.sha256(
            dispositions_bytes).hexdigest()
        live_snapshot_path = root / "owner-live-prefix.jsonl"
        live_snapshot_written = False

        fence = (
            commit_fence() if commit_fence is not None
            else nullcontext(lambda: None))
        try:
            with fence as assert_commit_authorized:
                with self.daemon.transaction() as conn:
                    _request_decision_id, current_request = (
                        self._review_request_in_txn(
                            conn, request_id, cycle_id=scope.cycle_id))
                    if current_request != request:
                        raise RuntimeMCPError("native review request 持久身份漂移")
                    duplicate = conn.execute(
                        "SELECT id FROM decision WHERE cycle_id=? "
                        "AND type='runtime_review' AND json_valid(payload_json) "
                        "AND json_extract(payload_json,'$.protocol')="
                        "'native-review-receipt-v1' "
                        "AND json_extract(payload_json,'$.review_request_id')=?",
                        (ci, request_id)).fetchall()
                    if duplicate:
                        raise RuntimeMCPError("native review request 已完成，拒绝重复 round")
                    chain = self._native_review_chain_in_txn(
                        conn, scope, request["review_kind"],
                        runner_call_id=runner_call_id,
                        parent_thread_id=parent_thread_id,
                        parent_turn_id=parent_turn_id)
                    if (
                            request["review_kind"]
                            in {"bundle_code", "bundle_result"}
                            and request["round_no"] == 1
                            and request["prior_receipt_hash"] is None
                            and len(chain)
                            == request["configured_rounds"]
                            and request["reviewed_subject_hash"]
                            != chain[-1][1]["resulting_subject_hash"]):
                        chain = []
                    expected_round = len(chain) + 1
                    expected_prior = (
                        chain[-1][1]["receipt_hash"] if chain else None)
                    expected_subject = (
                        chain[-1][1]["resulting_subject_hash"] if chain else
                        request["reviewed_subject_hash"])
                    if (request["round_no"] != expected_round
                            or request["prior_receipt_hash"] != expected_prior
                            or request["reviewed_subject_hash"] != expected_subject):
                        raise RuntimeMCPError(
                            "native review request stale/skipped/duplicate lineage")
                    if request["review_kind"] == "bundle_result":
                        current_owner = self._bundle_result_owner_candidate(
                            scope, conn=conn)
                        current_owner_hash = self._single_json_review_subject(
                            "bundle_result.json", current_owner)
                        if (current_owner_hash
                                != request["reviewed_subject_hash"]
                                or current_owner_hash != resulting_hash):
                            raise RuntimeMCPError(
                                "bundle_result owner subject 在 review "
                                "完成前发生漂移，请重新 prepare_review")
                    try:
                        claimed, live_snapshot = (
                            ledger.claim_completed_child_snapshot(
                                evidence.child_thread_id,
                                claim_id=request_id))
                    except NativeReviewError as error:
                        raise RuntimeMCPError(
                            f"native child claim 失败: {error}") from error
                    if claimed != evidence:
                        raise RuntimeMCPError("native child evidence claim 身份漂移")
                    self._atomic_payload_file(
                        live_snapshot_path, live_snapshot)
                    live_snapshot_written = True
                    live_snapshot_hash = (
                        "sha256:"
                        + hashlib.sha256(live_snapshot).hexdigest())
                    payload = {
                        "protocol": "native-review-receipt-v1",
                        "review_request_id": request_id,
                        "cycle_id": scope.cycle_id, "stage": scope.stage,
                        "target_id": scope.target_id, "purpose": scope.purpose,
                        "review_kind": request["review_kind"],
                        "round_no": request["round_no"],
                        "configured_rounds": request["configured_rounds"],
                        "reviewed_subject_hash": request["reviewed_subject_hash"],
                        "resulting_subject_hash": resulting_hash,
                        "prior_receipt_hash": request["prior_receipt_hash"],
                        "runner_call_id": runner_call_id,
                        "parent_thread_id": parent_thread_id,
                        "parent_turn_id": parent_turn_id,
                        "child_call_id": evidence.call_id,
                        "child_thread_id": evidence.child_thread_id,
                        "child_turn_id": evidence.child_turn_id,
                        "review_input_item_id": delivered.item_id,
                        "review_input_brief_hash":
                            delivered.reviewer_brief_hash,
                        "review_input_candidate_manifest_hash":
                            delivered.candidate_manifest_hash,
                        "verdict": result["verdict"],
                        "findings_ref": str(findings_path),
                        "findings_hash": findings_hash,
                        "dispositions_ref": str(dispositions_path),
                        "disposition_hash": disposition_hash,
                        "revised_candidate_manifest_ref": revised_manifest_ref,
                        "revised_candidate_manifest_hash": revised_manifest_hash,
                    }
                    payload["receipt_hash"] = self._receipt_hash(payload)
                    self._validate_native_receipt(payload)
                    decision_id = conn.execute(
                        "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                        "VALUES (?,?,'agent','runtime_review',?)",
                        (ci, active_q, json.dumps(
                            payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")))).lastrowid
                    live_proof = {
                        "protocol":
                            "native-review-live-owner-proof-v1",
                        "spawn_proof_mode":
                            ledger.spawn_proof_mode,
                        "review_decision_id": int(decision_id),
                        "review_receipt_hash": payload["receipt_hash"],
                        "review_request_id": request_id,
                        "cycle_id": scope.cycle_id,
                        "stage": scope.stage,
                        "target_id": scope.target_id,
                        "purpose": scope.purpose,
                        "review_kind": request["review_kind"],
                        "runner_call_id": runner_call_id,
                        "parent_thread_id": parent_thread_id,
                        "parent_turn_id": parent_turn_id,
                        "child_call_id": evidence.call_id,
                        "child_thread_id": evidence.child_thread_id,
                        "child_turn_id": evidence.child_turn_id,
                        "snapshot_ref": str(live_snapshot_path),
                        "snapshot_sha256": live_snapshot_hash,
                        "snapshot_bytes": len(live_snapshot),
                    }
                    conn.execute(
                        "INSERT INTO decision("
                        "cycle_id,question_id,actor,type,payload_json"
                        ") VALUES (?,?,'orchestrator',"
                        "'native_review_live_owner_proof',?)",
                        (ci, active_q, json.dumps(
                            live_proof, ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"))))
                    ack_decision_id = None
                    if (request["review_kind"] == "bundle_result"
                            and request["round_no"]
                            == request["configured_rounds"]):
                        ack_payload = {
                            "protocol":
                                "native-bundle-result-review-ack-v2",
                            "cycle_id": scope.cycle_id,
                            "build_target_id": int(scope.target_id),
                            "candidate_decision_id":
                                int(current_owner["candidate_decision_id"]),
                            "subject_hash": resulting_hash,
                            "configured_rounds":
                                request["configured_rounds"],
                            "review_decision_id": int(decision_id),
                            "review_receipt_hash": payload["receipt_hash"],
                            "runner_call_id": runner_call_id,
                            "parent_thread_id": parent_thread_id,
                            "parent_turn_id": parent_turn_id,
                            "purpose": scope.purpose,
                        }
                        ack_decision_id = conn.execute(
                            "INSERT INTO decision("
                            "cycle_id,question_id,actor,type,payload_json"
                            ") VALUES (?,?,'orchestrator',"
                            "'runtime_bundle_result_review_ack',?)",
                            (ci, active_q, json.dumps(
                                ack_payload, ensure_ascii=False,
                                sort_keys=True, separators=(",", ":"))),
                        ).lastrowid
                    assert_commit_authorized()
            return {
                "ok": True, "created": True,
                "decision_id": int(decision_id),
                "review_request_id": request_id,
                "round_no": request["round_no"],
                "reviewed_subject_hash": request["reviewed_subject_hash"],
                "resulting_subject_hash": resulting_hash,
                "verdict": result["verdict"],
                "receipt_hash": payload["receipt_hash"],
                **({"bundle_result_ack_decision_id": int(ack_decision_id)}
                   if ack_decision_id is not None else {}),
            }
        except BaseException:
            shutil.rmtree(revised_root, ignore_errors=True)
            try:
                findings_path.unlink()
            except FileNotFoundError:
                pass
            try:
                dispositions_path.unlink()
            except FileNotFoundError:
                pass
            if live_snapshot_written:
                try:
                    live_snapshot_path.unlink()
                except FileNotFoundError:
                    pass
            raise

    def _record_cycle_summary(self, scope: RuntimeMCPScope,
                              arguments: Dict[str, Any]) -> Dict[str, Any]:
        if scope.stage != "reasoning":
            raise RuntimeMCPError("record_cycle_summary 只允许 reasoning 主阶段调用")
        goal_id, goal_ver, active_q, _status = self._cycle_row(scope)
        ci = _cycle_number(scope.cycle_id)
        conclusion = _bounded_text(
            arguments.get("conclusion_md"), name="conclusion_md", maximum=262144)
        decision = arguments.get("decision")
        if decision not in {"continue", "decompose", "replan", "terminate", "inconclusive"}:
            raise RuntimeMCPError("cycle summary decision 非法")
        next_step = _bounded_text(
            arguments.get("next_step_md"), name="next_step_md", maximum=65536,
            allow_empty=True)
        refs = arguments.get("evidence_refs", [])
        if (not isinstance(refs, list) or len(refs) > 256
                or any(not isinstance(item, str) or not item.strip()
                       or len(item.encode("utf-8")) > 4096 for item in refs)):
            raise RuntimeMCPError("evidence_refs 须为最多 256 条短文本")
        summary = {
            "protocol": "runtime-cycle-summary-v1",
            "goal_id": goal_id, "goal_ver": goal_ver,
            "question_id": active_q, "conclusion_md": conclusion,
            "decision": decision, "next_step_md": next_step,
            "evidence_refs": list(refs),
        }
        with self.daemon.transaction() as conn:
            rows = conn.execute(
                "SELECT id,payload_json FROM decision WHERE cycle_id=? "
                "AND actor='agent' AND type='runtime_cycle_summary' ORDER BY id",
                (ci,),
            ).fetchall()
            for decision_id, payload_json in rows:
                existing = json.loads(payload_json)
                existing_without_revision = dict(existing)
                existing_without_revision.pop("revision", None)
                if existing_without_revision == summary:
                    return {"ok": True, "created": False,
                            "decision_id": int(decision_id), **existing}
            revision = len(rows) + 1
            payload = {**summary, "revision": revision}
            decision_id = conn.execute(
                "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                "VALUES (?,?,'agent','runtime_cycle_summary',?)",
                (ci, active_q, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            ).lastrowid
        return {"ok": True, "created": True,
                "decision_id": int(decision_id), **payload}

    # -- resident stage artifact submission ---------------------------------
    @staticmethod
    def _canonical_bytes(value: Any) -> bytes:
        try:
            return (json.dumps(
                value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError) as error:
            raise RuntimeMCPError(
                f"阶段产物不是可持久化的严格 JSON: {error}") from error

    @staticmethod
    def _safe_artifact_name(name: Any) -> str:
        if (not isinstance(name, str) or not name or len(name.encode("utf-8")) > 4096
                or name.startswith("/") or "\\" in name or "\x00" in name):
            raise RuntimeMCPError(f"阶段产物文件名非法: {name!r}")
        parts = name.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise RuntimeMCPError(f"阶段产物文件名须为规范相对路径: {name!r}")
        return name

    def _schema_errors(self, schema_name: str, payload: Any, *, artifact: bool = False) -> list[str]:
        if self.schemas is None:
            raise RuntimeMCPError("runtime MCP 未装配 SchemaSet，不能提交阶段产物")
        try:
            validator = (self.schemas.validator_for_artifact(schema_name)
                         if artifact else self.schemas.validator(schema_name))
        except KeyError as error:
            raise RuntimeMCPError(f"阶段产物 schema 不存在: {schema_name}") from error
        errors: list[str] = []
        for item in validator.iter_errors(payload):
            errors.append(f"{item.json_path} {item.message}")
            pending = list(item.context or [])
            while pending:
                child = pending.pop()
                errors.append(f"{child.json_path} {child.message}")
                pending.extend(child.context or [])
        return errors

    def _require_schema(self, filename: str, payload: Any, *, short_name: Optional[str] = None) -> None:
        errors = self._schema_errors(
            short_name or filename, payload, artifact=short_name is None)
        if errors:
            raise RuntimeMCPError(
                f"{filename} schema 校验失败:\n" + "\n".join(errors[:16]))

    def _collect_stage_files(
            self, scope: RuntimeMCPScope, arguments: Dict[str, Any],
            *, store_key: str) -> tuple[Dict[str, Any], Optional[Path]]:
        unknown = set(arguments) - {"files", "workspace_files", "md"}
        if unknown:
            raise RuntimeMCPError(f"submit_stage_artifact 含未知参数: {sorted(unknown)}")
        raw_files = arguments.get("files")
        if not isinstance(raw_files, dict) or len(raw_files) > 1024:
            raise RuntimeMCPError("files 须为不超过 1024 项的 object")
        files: Dict[str, Any] = {}
        for raw_name, value in raw_files.items():
            name = self._safe_artifact_name(raw_name)
            if name in files:
                raise RuntimeMCPError(f"阶段产物文件名重复: {name}")
            files[name] = value

        workspace_files = arguments.get("workspace_files", [])
        if not isinstance(workspace_files, list) or len(workspace_files) > 1024:
            raise RuntimeMCPError("workspace_files 须为不超过 1024 项的数组")
        if not workspace_files:
            return files, None
        if scope.stage != "bundle":
            raise RuntimeMCPError("workspace_files 只允许 Bundle 主阶段提交")
        if scope.workspace_root is None or scope.output_uid is None or self.work_root is None:
            raise RuntimeMCPError("Bundle MCP 提交缺当前一次性 workspace/file-manager 能力")
        runtime_dir = Path(scope.workspace_root).resolve(strict=True)
        managed_root = self.work_root / "runtime" / "stage-submissions" / ".incoming"
        try:
            from .runner import (
                _promote_workspace_submissions,
                _read_managed_control_file,
            )
            envelope = "```json\n" + json.dumps({
                "files": {}, "workspace_files": workspace_files, "md": "",
            }, ensure_ascii=False, sort_keys=True) + "\n```"
            promoted = _promote_workspace_submissions(
                envelope, runtime_dir=runtime_dir, managed_root=managed_root,
                expected_uid=int(scope.output_uid), store_key=store_key)
        except Exception as error:
            raise RuntimeMCPError(f"Bundle workspace 文件接收失败: {error}") from error
        promoted_dir = managed_root / store_key
        duplicates = sorted(set(files) & set(promoted))
        if duplicates:
            shutil.rmtree(promoted_dir, ignore_errors=True)
            raise RuntimeMCPError(
                f"Bundle 文件同时内联并通过 workspace 提交: {duplicates}")
        files.update(promoted)
        try:
            for control_name in ("execution_manifest.json", "identity.md"):
                if control_name in promoted:
                    files[control_name] = _read_managed_control_file(
                        promoted[control_name], logical_name=control_name)
        except Exception as error:
            shutil.rmtree(promoted_dir, ignore_errors=True)
            raise RuntimeMCPError(f"Bundle 控制文件不可解析: {error}") from error
        return files, promoted_dir

    def _validate_stage_files(self, scope: RuntimeMCPScope,
                              files: Dict[str, Any]) -> str:
        names = set(files)
        operator_turn = scope.stage == "bundle" and "-operator-" in scope.purpose
        if names == {"resource_request.json"}:
            if operator_turn:
                raise RuntimeMCPError("Bundle operator turn 不接受 resource_request")
            self._require_schema(
                "resource_request.json", files["resource_request.json"],
                short_name="resource_request")
            return "resource_request"
        if scope.stage == "idea":
            if names != {"idea_set.json"}:
                raise RuntimeMCPError(
                    f"Idea 最终提交必须且只能包含 idea_set.json；实收 {sorted(names)}")
            self._require_schema("idea_set.json", files["idea_set.json"])
            return "idea"
        if scope.stage == "plan":
            if names != {"plan.json"}:
                raise RuntimeMCPError(
                    "resident Plan 最终提交必须且只能包含 plan.json；"
                    "检索须在当前 turn 调用 plan_import_search，不能以 sidecar 结束阶段；"
                    f"实收 {sorted(names)}")
            self._require_schema("plan.json", files["plan.json"])
            gpu_policy = ((self.policy or {}).get("resources") or {}).get(
                "gpu_target_policy", "planner_select")
            if gpu_policy in {"required", "forbidden"}:
                expected_gpu = gpu_policy == "required"
                mismatches = [
                    target.get("target_key", f"#{index + 1}")
                    for index, target in enumerate(files["plan.json"].get("targets", []))
                    if not isinstance(target, dict)
                    or target.get("gpu_required") is not expected_gpu
                ]
                if mismatches:
                    raise RuntimeMCPError(
                        "plan GPU mode 与本任务固定计算选择不一致；"
                        f"期望 gpu_required={str(expected_gpu).lower()}；"
                        f"targets={mismatches}")
            ci = _cycle_number(scope.cycle_id)
            seen_claims: set[str] = set()
            for target in files["plan.json"].get("targets", []):
                if target.get("target_kind") != "build":
                    continue
                claim = target.get("claim") or {}
                canonical = claim.get("canonical_key")
                slug = claim.get("slug")
                if (_SAFE_KEY.fullmatch(str(canonical or "")) is None
                        or _SAFE_KEY.fullmatch(str(slug or "")) is None):
                    raise RuntimeMCPError(
                        "build claim canonical_key/slug 须为 ASCII 安全索引键；"
                        f"target={target.get('target_key')!r}")
                if canonical in seen_claims:
                    raise RuntimeMCPError(
                        f"plan 内 build canonical_key 重复: {canonical}")
                seen_claims.add(canonical)
                existing = self.daemon.query_one(
                    "SELECT slug,born_cycle,status FROM baseline WHERE canonical_key=?",
                    (canonical,))
                if existing is not None and (
                        existing[0] != slug or existing[1] != ci):
                    raise RuntimeMCPError(
                        f"baseline identity 冲突: canonical_key={canonical!r} 已由 "
                        f"cycle c{existing[1]} 以 slug={existing[0]!r} "
                        f"status={existing[2]!r} 占用；请复用或更换 build identity")
            if self.plan_controller is not None:
                try:
                    self.plan_controller.preflight_plan_session(scope, files["plan.json"])
                except Exception as error:
                    raise RuntimeMCPError(
                        f"plan 语义预检失败: {error}") from error
            return "plan"
        if scope.stage == "reasoning":
            allowed = {"selection.json", "tree_ops.json", "answer.json"}
            if "selection.json" not in names or not names.issubset(allowed):
                raise RuntimeMCPError(
                    "Reasoning 必须包含 selection.json，且只可附带 tree_ops.json/answer.json；"
                    f"实收 {sorted(names)}")
            for name in sorted(names):
                self._require_schema(name, files[name])
            if self.reasoning_controller is not None:
                try:
                    self.reasoning_controller.preflight_reasoning_session(
                        scope, files)
                except Exception as error:
                    raise RuntimeMCPError(
                        f"reasoning 语义预检失败: {error}") from error
            return "reasoning"
        if scope.stage != "bundle":
            raise RuntimeMCPError(f"未知阶段: {scope.stage!r}")
        if operator_turn or names == {"bundle_operator_action.json"}:
            if names != {"bundle_operator_action.json"}:
                raise RuntimeMCPError(
                    "Bundle operator 只能提交 bundle_operator_action.json")
            self._require_schema(
                "bundle_operator_action.json", files["bundle_operator_action.json"])
            return "bundle_operator"
        required = {"execution_manifest.json", "identity.md"}
        if not required.issubset(names):
            raise RuntimeMCPError(
                f"Bundle 缺必需文件 {sorted(required - names)}；实收 {sorted(names)}")
        manifest = files["execution_manifest.json"]
        self._require_schema("execution_manifest.json", manifest)
        identity = files["identity.md"]
        if not isinstance(identity, str) or not identity.strip():
            raise RuntimeMCPError("identity.md 须为非空文本")
        code_names = manifest.get("code_files", []) if isinstance(manifest, dict) else []
        expected = required | set(code_names)
        if names != expected:
            raise RuntimeMCPError(
                "Bundle files 必须与 manifest.code_files 完全闭合；"
                f"缺少 {sorted(expected - names)}，多出 {sorted(names - expected)}")
        if scope.target_id is None:
            raise RuntimeMCPError("Bundle 提交缺 build_target_id")
        try:
            target_id = int(scope.target_id)
        except (TypeError, ValueError) as error:
            raise RuntimeMCPError("Bundle build_target_id 非法") from error
        row = self.daemon.query_one(
            "SELECT cycle_id,plan_ref FROM build_target WHERE id=?", (target_id,))
        if row is None or row[0] != _cycle_number(scope.cycle_id) or row[1] is None:
            raise RuntimeMCPError("Bundle build_target 不属于当前 cycle 或缺冻结 plan_ref")
        try:
            from . import manifest as manifest_module
            plan_slice = json.loads(row[1])
            manifest_module.cross_check(manifest, plan_slice)
        except Exception as error:
            raise RuntimeMCPError(f"Bundle manifest 与冻结 plan 不一致: {error}") from error
        return "bundle"

    @staticmethod
    def _atomic_payload_file(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(tmp, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short stage-submission write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        dir_fd = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _assert_submission_binding_in_txn(
            self, conn: sqlite3.Connection, scope: RuntimeMCPScope,
            submission_kind: str, files: Mapping[str, Any]) -> None:
        """Recheck stage/cycle/target authority at the commit linearization point."""
        ci = _cycle_number(scope.cycle_id)
        row = conn.execute(
            "SELECT status,route FROM cycle WHERE id=?", (ci,)).fetchone()
        if row is None:
            raise RuntimeMCPError(f"当前 cycle 不存在: {scope.cycle_id}")
        status, route = str(row[0]), row[1]
        if status in {"done", "failed", "aborted"}:
            raise RuntimeMCPError(
                f"cycle {scope.cycle_id} 已终态，拒绝阶段提交")
        expected = {
            "idea": {"created"},
            "plan": {"idea"},
            "bundle": {"plan"},
            # Bootstrap/decompose/goal_amend are reasoning-only cycles and
            # legitimately submit while the cursor is still ``created``.
            "reasoning": {"created", "bundle"},
        }.get(scope.stage)
        if expected is None or status not in expected:
            raise RuntimeMCPError(
                f"阶段提交绑定已漂移: stage={scope.stage!r}, "
                f"cycle.status={status!r}, route={route!r}")

        if scope.stage != "bundle":
            if scope.target_id is not None:
                raise RuntimeMCPError("非 Bundle 阶段不得绑定 build_target")
            return
        if scope.target_id is None:
            raise RuntimeMCPError("Bundle 提交缺 build_target_id")
        try:
            target_id = int(scope.target_id)
        except (TypeError, ValueError) as error:
            raise RuntimeMCPError("Bundle build_target_id 非法") from error
        target = conn.execute(
            "SELECT cycle_id,status,plan_ref FROM build_target WHERE id=?",
            (target_id,)).fetchone()
        if (target is None or int(target[0]) != ci or target[2] is None
                or str(target[1]) in {
                    "complete", "skipped", "failed", "engineering_blocked"}):
            raise RuntimeMCPError(
                "Bundle build_target 绑定已漂移、缺冻结 plan_ref 或已终态")
        if submission_kind == "bundle":
            try:
                from . import manifest as manifest_module
                manifest_module.cross_check(
                    files["execution_manifest.json"], json.loads(target[2]))
            except Exception as error:
                raise RuntimeMCPError(
                    f"Bundle manifest 在提交点已不匹配冻结 plan: {error}") from error

    def _required_review_kind_in_txn(
            self, conn: sqlite3.Connection, scope: RuntimeMCPScope,
            review_kind: str, artifact_hash: str,
            ) -> Optional[tuple[int, Dict[str, Any]]]:
        """Require exactly N live native receipts ending at one subject hash."""
        rounds = self._configured_review_rounds(scope, review_kind)
        if rounds == 0:
            return None
        ledger, runner_call_id, parent_thread_id, parent_turn_id = (
            self._native_review_scope(scope))
        chain = self._native_review_chain_in_txn(
            conn, scope, review_kind, runner_call_id=runner_call_id,
            parent_thread_id=parent_thread_id,
            parent_turn_id=parent_turn_id)
        if len(chain) != rounds:
            raise RuntimeMCPError(
                f"{review_kind} 最终门前须完成精确 {rounds} 轮 native review；"
                f"当前 {len(chain)} 轮。请先 prepare_review/record_review")
        for _decision_id, receipt in chain:
            self._verify_native_receipt_child_binding(ledger, receipt)
        if chain[-1][1]["resulting_subject_hash"] != artifact_hash:
            raise RuntimeMCPError(
                f"{review_kind} subject hash 不等于 final review hash；"
                "请提交第 N 轮 resulting candidate")
        return chain[-1]

    def _required_review_in_txn(
            self, conn: sqlite3.Connection, scope: RuntimeMCPScope,
            submission_kind: str, artifact_hash: str,
            files: Optional[Mapping[str, Any]] = None) -> Optional[int]:
        """Require exactly N native receipts ending at this stage artifact."""
        if submission_kind == "idea" and self.wildidea_adapter is not None:
            route_row = self._idea_route_in_txn(conn, scope)
            if route_row is None:
                raise RuntimeMCPError(
                    "Idea 最终提交前必须调用 wildidea_expand，"
                    "由服务端绑定 generation_path")
            route_decision_id, route = route_row
            idea_set = (files or {}).get("idea_set.json")
            if not isinstance(idea_set, Mapping):
                raise RuntimeMCPError(
                    "Idea generation_path gate 缺 idea_set.json")
            self._assert_idea_matches_route(idea_set, route)
            if route["generation_path"] == "wildidea":
                result = self._wildidea_result_in_txn(
                    conn, scope, route_decision_id,
                    route["receipt_hash"])
                if result is None:
                    raise RuntimeMCPError(
                        "generation_path=wildidea 最终提交前必须调用 "
                        "wildidea_audit 并吸收其 internal audit 结果")
                decision_id, receipt = result
                if receipt["artifact_hash"] != artifact_hash:
                    raise RuntimeMCPError(
                        "提交的 idea_set 与 WildIdea internal audit "
                        "结果 hash 不一致")
                ledger, runner_call_id, parent_thread_id, parent_turn_id = (
                    self._native_review_scope(scope))
                del ledger
                native_chain = self._native_review_chain_in_txn(
                    conn, scope, "idea",
                    runner_call_id=runner_call_id,
                    parent_thread_id=parent_thread_id,
                    parent_turn_id=parent_turn_id)
                if native_chain:
                    raise RuntimeMCPError(
                        "WildIdea 路径不得混入 native child review")
                return decision_id
        review_kind = {
            "idea": "idea", "plan": "plan", "bundle": "bundle_code",
        }.get(submission_kind)
        if review_kind is None:
            return None
        final = self._required_review_kind_in_txn(
            conn, scope, review_kind, artifact_hash)
        return None if final is None else final[0]

    @staticmethod
    def _validate_bundle_result_review_ack(payload: Dict[str, Any]) -> None:
        required = {
            "protocol", "cycle_id", "build_target_id", "subject_hash",
            "candidate_decision_id", "configured_rounds",
            "review_decision_id",
            "review_receipt_hash", "runner_call_id", "parent_thread_id",
            "parent_turn_id", "purpose",
        }
        if (set(payload) != required
                or payload.get("protocol")
                != "native-bundle-result-review-ack-v2"):
            raise RuntimeMCPError(
                "bundle_result review ack 字段闭包/协议非法")
        for field in (
                "build_target_id", "candidate_decision_id",
                "configured_rounds",
                "review_decision_id", "runner_call_id"):
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RuntimeMCPError(
                    f"bundle_result review ack {field} 非法")
        for field in ("subject_hash", "review_receipt_hash"):
            if _SHA256.fullmatch(str(payload.get(field) or "")) is None:
                raise RuntimeMCPError(
                    f"bundle_result review ack {field} 非法")
        for field in (
                "cycle_id", "parent_thread_id", "parent_turn_id", "purpose"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise RuntimeMCPError(
                    f"bundle_result review ack {field} 非法")

    def _require_bundle_result_review_ack_in_txn(
            self, conn: sqlite3.Connection,
            scope: RuntimeMCPScope) -> Optional[int]:
        """Recompute one complete target and require its current native ack."""
        rounds = self._configured_review_rounds(scope, "bundle_result")
        if rounds == 0:
            return None
        owner_candidate = self._bundle_result_owner_candidate(
            scope, conn=conn)
        subject_hash = self._single_json_review_subject(
            "bundle_result.json", owner_candidate)
        final = self._required_review_kind_in_txn(
            conn, scope, "bundle_result", subject_hash)
        if final is None:  # rounds > 0 makes this defensive only.
            raise RuntimeMCPError(
                "bundle_result review ack 缺失")
        review_decision_id, receipt = final
        _ledger, runner_call_id, parent_thread_id, parent_turn_id = (
            self._native_review_scope(scope))
        expected = {
            "protocol": "native-bundle-result-review-ack-v2",
            "cycle_id": scope.cycle_id,
            "build_target_id": int(scope.target_id),
            "candidate_decision_id":
                owner_candidate["candidate_decision_id"],
            "subject_hash": subject_hash,
            "configured_rounds": rounds,
            "review_decision_id": review_decision_id,
            "review_receipt_hash": receipt["receipt_hash"],
            "runner_call_id": runner_call_id,
            "parent_thread_id": parent_thread_id,
            "parent_turn_id": parent_turn_id,
            "purpose": scope.purpose,
        }
        rows = conn.execute(
            "SELECT id,payload_json FROM decision "
            "WHERE cycle_id=? AND actor='orchestrator' "
            "AND type='runtime_bundle_result_review_ack' ORDER BY id",
            (_cycle_number(scope.cycle_id),)).fetchall()
        matches = []
        for decision_id, raw in rows:
            payload = _strict_json_object(
                raw, label="bundle_result review ack")
            self._validate_bundle_result_review_ack(payload)
            if payload == expected:
                matches.append(int(decision_id))
        if len(matches) != 1:
            raise RuntimeMCPError(
                "当前 complete target 缺唯一、owner-bound 的 "
                "bundle_result review ack")
        return matches[0]

    def assert_stage_turn_complete(self, scope: RuntimeMCPScope) -> None:
        """Owner-side normal-exit postcondition for a resident stage turn."""
        if scope.stage != "bundle":
            return
        role = self._bundle_role(scope)
        if role == "scheduler":
            controller = self._require_bundle_scheduler(scope)
            try:
                overview = controller.bundle_scheduler_overview(scope)
            except Exception as error:
                raise RuntimeMCPError(str(error)) from error
            if (not isinstance(overview, Mapping)
                    or overview.get("cycle_terminal") is not True
                    or overview.get("drained") is not True
                    or overview.get("controller_error") not in (None, "")):
                raise RuntimeMCPError(
                    "Bundle Scheduler 尚未证明 cycle 终态与安全排空")
            return
        if role == "worker":
            controller = self._require_bundle_controller(scope)
            try:
                status = controller.bundle_session_status(
                    scope, mode="snapshot", after_seq=0,
                    limit=200, timeout_s=0)
            except TypeError:
                # Compatibility is deliberately limited to the controller
                # boundary; new production controllers implement the cursor
                # contract.
                status = controller.bundle_session_status(scope)
            except Exception as error:
                raise RuntimeMCPError(str(error)) from error
            if (not isinstance(status, Mapping)
                    or status.get("terminal") is not True
                    or status.get("worker_running") is True
                    or status.get("controller_error") not in (None, "")):
                raise RuntimeMCPError(
                    "Bundle Target Worker 尚未证明 target 终态")
            if self._configured_review_rounds(
                    scope, "bundle_result") == 0:
                return
            with self.daemon.transaction() as conn:
                self._require_bundle_result_review_ack_in_txn(
                    conn, scope)
            return
        if self._configured_review_rounds(scope, "bundle_result") == 0:
            return
        ci = _cycle_number(scope.cycle_id)
        with self.daemon.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM build_target "
                "WHERE cycle_id=? AND status='complete' ORDER BY seq,id",
                (ci,)).fetchall()
            for (target_id,) in rows:
                self._require_bundle_result_review_ack_in_txn(
                    conn, replace(scope, target_id=str(int(target_id))))

    def _persist_stage_submission(
            self, scope: RuntimeMCPScope, files: Dict[str, Any], md: str,
            *, submission_kind: str, incoming_dir: Optional[Path],
            store_key: str,
            commit_fence: Optional[
                Callable[[], ContextManager[Callable[[], None]]]] = None,
            ) -> Dict[str, Any]:
        if self.work_root is None:
            raise RuntimeMCPError("runtime MCP 未装配 work_root，不能持久化阶段产物")
        ci = _cycle_number(scope.cycle_id)
        target_key = f"t{scope.target_id}" if scope.target_id is not None else "stage"
        root = (self.work_root / "runtime" / "stage-submissions" /
                f"c{ci}" / scope.stage / target_key / store_key)
        if root.exists() or root.is_symlink():
            raise RuntimeMCPError("阶段提交目录身份冲突")
        root.mkdir(parents=True, mode=0o700)
        entries: list[Dict[str, Any]] = []
        materialized: Dict[str, Any] = dict(files)
        try:
            if submission_kind == "bundle":
                from . import manifest as manifest_module
                from .interfaces import ManagedArtifactRef
                manifest = files["execution_manifest.json"]
                actual_refs = manifest_module.extract_manifest_asset_refs(manifest)
                kwargs: Dict[str, Any] = {}
                if actual_refs:
                    if not scope.pack_hash or self.work_root is None:
                        raise RuntimeMCPError(
                            "Bundle 使用输入资产但 MCP scope 缺 ContextPack 身份")
                    identities = manifest_module.capture_asset_identities(
                        actual_refs, work_root=self.work_root)
                    kwargs = {
                        "authorization_pack_hash": scope.pack_hash,
                        "allowed_asset_refs": list(scope.refs),
                        "asset_identities": identities,
                    }
                bundle_root = root / "bundle"
                ledger = manifest_module.stage_bundle_files(
                    files, manifest, bundle_root, **kwargs)
                materialized = {
                    "execution_manifest.json": manifest,
                    "identity.md": files["identity.md"],
                }
                for name in manifest["code_files"]:
                    path = bundle_root.joinpath(*name.split("/"))
                    materialized[name] = ManagedArtifactRef(
                        path=str(path), size_bytes=path.stat().st_size,
                        sha256="sha256:" + ledger[name])

            for name in sorted(materialized):
                value = materialized[name]
                if hasattr(value, "path") and hasattr(value, "sha256"):
                    path = Path(value.path)
                    size = int(value.size_bytes)
                    digest = str(value.sha256)
                    kind = "managed"
                else:
                    if name.endswith(".json"):
                        payload = self._canonical_bytes(value)
                        kind = "json"
                    elif isinstance(value, str):
                        payload = value.encode("utf-8")
                        kind = "text"
                    else:
                        payload = self._canonical_bytes(value)
                        kind = "json"
                    path = root / "files" / Path(*name.split("/"))
                    self._atomic_payload_file(path, payload)
                    size = len(payload)
                    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
                entries.append({
                    "name": name, "kind": kind, "path": str(path),
                    "size_bytes": size, "sha256": digest,
                })

            md_entry = None
            if md:
                md_bytes = md.encode("utf-8")
                md_path = root / "stage.md"
                self._atomic_payload_file(md_path, md_bytes)
                md_entry = {
                    "path": str(md_path), "size_bytes": len(md_bytes),
                    "sha256": "sha256:" + hashlib.sha256(md_bytes).hexdigest(),
                }
            descriptor = {
                "files": [{key: item[key] for key in (
                    "name", "kind", "size_bytes", "sha256")} for item in entries],
                "md": (None if md_entry is None else {
                    "size_bytes": md_entry["size_bytes"],
                    "sha256": md_entry["sha256"],
                }),
            }
            artifact_hash = "sha256:" + hashlib.sha256(
                self._canonical_bytes(descriptor)).hexdigest()
            fence = (
                commit_fence() if commit_fence is not None
                else nullcontext(lambda: None))
            # Revision allocation and its decision insert share the daemon's one
            # short write transaction.  The broker fence is held across this
            # commit edge, so revoke cannot linearize between authorization and
            # persistence.  Payload materialization above remains outside the
            # transaction; only the small receipt/index write is fenced here.
            with fence as assert_commit_authorized:
                with self.daemon.transaction() as conn:
                    # All expensive parsing/materialization happened outside
                    # SQLite. Re-read mutable authority in this final short
                    # transaction so a stale turn cannot submit after another
                    # actor terminalized or advanced the cycle/target.
                    self._assert_submission_binding_in_txn(
                        conn, scope, submission_kind, files)
                    review_decision_id = self._required_review_in_txn(
                        conn, scope, submission_kind, artifact_hash,
                        files=files)
                    prior = conn.execute(
                        "SELECT coalesce(max(CAST(json_extract(payload_json,'$.revision') "
                        "AS INTEGER)),0) FROM decision "
                        "WHERE cycle_id=? AND actor='agent' "
                        "AND type='runtime_stage_submission' "
                        "AND json_valid(payload_json) "
                        "AND json_type(payload_json,'$.revision')='integer' "
                        "AND json_extract(payload_json,'$.stage')=? "
                        "AND coalesce(json_extract(payload_json,'$.target_id'),'')=?",
                        (ci, scope.stage, scope.target_id or "")).fetchone()
                    revision = int(prior[0]) + 1
                    receipt = {
                        "protocol": "runtime-stage-submission-v1",
                        "cycle_id": scope.cycle_id, "stage": scope.stage,
                        "target_id": scope.target_id, "purpose": scope.purpose,
                        "pack_hash": scope.pack_hash or None,
                        "submission_kind": submission_kind,
                        "review_decision_id": review_decision_id,
                        "revision": revision, "artifact_hash": artifact_hash,
                        "files": entries, "md": md_entry,
                    }
                    from .process_supervisor import atomic_write_receipt
                    receipt_path = root / "submission.json"
                    atomic_write_receipt(receipt_path, receipt)
                    receipt_bytes = receipt_path.read_bytes()
                    receipt_hash = "sha256:" + hashlib.sha256(
                        receipt_bytes).hexdigest()
                    decision_payload = {
                        "protocol": "runtime-stage-submission-index-v1",
                        "stage": scope.stage, "target_id": scope.target_id,
                        "purpose": scope.purpose, "revision": revision,
                        "review_decision_id": review_decision_id,
                        "artifact_hash": artifact_hash,
                        "submission_ref": str(receipt_path),
                        "submission_hash": receipt_hash,
                        "file_names": [item["name"] for item in entries],
                    }
                    decision_id = conn.execute(
                        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                        "VALUES (?,'agent','runtime_stage_submission',?)",
                        (ci, json.dumps(
                            decision_payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")))).lastrowid
                    # Recheck expiry at the end of the short body.  The broker
                    # lock remains held until COMMIT, so revoke cannot pass this
                    # check and then let an old request land behind it.
                    assert_commit_authorized()
            return {
                "ok": True, "decision_id": int(decision_id),
                "revision": revision, "artifact_hash": artifact_hash,
                "submission_ref": str(receipt_path),
                "submission_hash": receipt_hash,
                "file_names": decision_payload["file_names"],
            }
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        finally:
            if incoming_dir is not None:
                shutil.rmtree(incoming_dir, ignore_errors=True)

    def _submit_stage_artifact(
            self, scope: RuntimeMCPScope, arguments: Dict[str, Any], *,
            commit_fence: Optional[
                Callable[[], ContextManager[Callable[[], None]]]] = None,
            ) -> Dict[str, Any]:
        self._cycle_row(scope)
        if self.schemas is None or self.work_root is None:
            raise RuntimeMCPError(
                "runtime MCP 阶段提交能力未装配（缺 schemas/work_root）")
        md = arguments.get("md", "")
        if not isinstance(md, str) or len(md.encode("utf-8")) > 262144:
            raise RuntimeMCPError("md 须为不超过 262144 bytes 的字符串")
        store_key = f"s{time.time_ns()}-{secrets.token_hex(8)}"
        files, incoming = self._collect_stage_files(
            scope, arguments, store_key=store_key)
        try:
            kind = self._validate_stage_files(scope, files)
            return self._persist_stage_submission(
                scope, files, md, submission_kind=kind,
                incoming_dir=incoming, store_key=store_key,
                commit_fence=commit_fence)
        except BaseException:
            if incoming is not None:
                shutil.rmtree(incoming, ignore_errors=True)
            raise

    def load_stage_submission(self, submission_ref: str,
                              submission_hash: str) -> Dict[str, Any]:
        """Load one broker-issued submission as trusted path-backed files."""
        if self.work_root is None:
            raise RuntimeMCPError("runtime MCP 未装配 work_root")
        try:
            from .artifact_capability import open_artifact, read_artifact_bytes
            from .interfaces import ManagedArtifactRef
            receipt_path = Path(submission_ref).resolve(strict=True)
            storage_root = (
                self.work_root / "runtime" / "stage-submissions").resolve(strict=True)
            if os.path.commonpath((str(receipt_path), str(storage_root))) != str(storage_root):
                raise RuntimeMCPError("阶段提交回执逃逸 file-manager 根")
            raw = read_artifact_bytes(
                receipt_path, expected_hash=submission_hash,
                max_bytes=4 * 1024 * 1024, label="runtime stage submission receipt")
            receipt = json.loads(raw.decode("utf-8"))
            if receipt.get("protocol") != "runtime-stage-submission-v1":
                raise RuntimeMCPError("阶段提交回执协议非法")
            files: Dict[str, Any] = {}
            for entry in receipt.get("files", []):
                name = self._safe_artifact_name(entry.get("name"))
                path = Path(entry["path"])
                size = entry["size_bytes"]
                digest = entry["sha256"]
                kind = entry["kind"]
                if kind == "managed":
                    with open_artifact(
                            path, expected_hash=digest, expected_size=size,
                            label=f"runtime stage managed artifact {name}"):
                        pass
                    files[name] = ManagedArtifactRef(
                        path=str(path), size_bytes=size, sha256=digest)
                    continue
                body = read_artifact_bytes(
                    path, expected_hash=digest, expected_size=size,
                    max_bytes=max(4 * 1024 * 1024, size),
                    label=f"runtime stage artifact {name}")
                files[name] = (json.loads(body.decode("utf-8"))
                               if kind == "json" else body.decode("utf-8"))
            md = ""
            md_entry = receipt.get("md")
            if isinstance(md_entry, dict):
                md = read_artifact_bytes(
                    Path(md_entry["path"]), expected_hash=md_entry["sha256"],
                    expected_size=md_entry["size_bytes"],
                    max_bytes=262144, label="runtime stage markdown").decode("utf-8")
            return {
                "files": files, "md": md,
                "submission_ref": str(receipt_path),
                "artifact_hash": receipt["artifact_hash"],
                "target_id": receipt.get("target_id"),
                "pack_hash": receipt.get("pack_hash"),
            }
        except RuntimeMCPError:
            raise
        except Exception as error:
            raise RuntimeMCPError(f"阶段提交回执不可读取: {error}") from error


class _UnixBrokerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class RuntimeMCPBroker:
    """Owner-process broker issuing one capability per live Codex turn."""

    def __init__(self, service: RuntimeIngestService, *, runtime_root: Optional[Path] = None):
        self.service = service
        self.runtime_root = (
            None if runtime_root is None else Path(runtime_root).resolve(strict=False))
        self._lock = threading.RLock()
        self._grants: Dict[str, RuntimeMCPScope] = {}
        self._stage_submissions: Dict[str, tuple[str, str]] = {}
        self._submit_sequences: Dict[str, int] = {}
        self._bundle_cycle_complete: set[str] = set()
        self._directory: Optional[Path] = None
        # Public form is either an ordinary absolute path or ``@name`` for a
        # Linux abstract AF_UNIX address.  The latter avoids the kernel's very
        # small sockaddr_un pathname limit when a quest lives under a long
        # VEPFS project root; it is an in-kernel endpoint, not a file on /tmp.
        self._socket_path: Optional[str] = None
        self._server: Optional[_UnixBrokerServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def socket_path(self) -> str:
        if self._socket_path is None:
            raise RuntimeError("runtime MCP broker 尚未启动")
        return self._socket_path

    def start(self) -> "RuntimeMCPBroker":
        if self._server is not None:
            return self
        directory = None
        socket_address: str
        public_address: str
        # Leave margin for CPython/platform sockaddr_un details.  A normal
        # filesystem socket is useful in short test/deployment roots; long
        # project paths transparently switch to the Linux abstract namespace.
        # ``runtime_root=None`` still means tempfile.gettempdir(), which may
        # itself be the installation's long VEPFS path after storage binding.
        # Include that real destination in the pathname-limit decision.
        root_hint = str(
            self.runtime_root
            if self.runtime_root is not None else Path(tempfile.gettempdir()))
        use_abstract = (
            sys.platform.startswith("linux")
            and len(os.fsencode(root_hint)) + 64 >= 100)
        if use_abstract:
            name = f"meta-research-mcp-{os.getpid()}-{secrets.token_hex(12)}"
            socket_address = "\0" + name
            public_address = "@" + name
        else:
            if self.runtime_root is not None:
                self.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(self.runtime_root, 0o700)
            directory = Path(tempfile.mkdtemp(
                prefix="meta-research-runtime-mcp-",
                dir=(str(self.runtime_root) if self.runtime_root is not None else None)))
            os.chmod(directory, 0o711)
            socket_path = directory / "broker.sock"
            socket_address = str(socket_path)
            public_address = socket_address
        broker = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                raw = self.rfile.readline(_MAX_MESSAGE_BYTES + 1)
                if not raw or len(raw) > _MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
                    return
                response: Dict[str, Any]
                try:
                    request = json.loads(raw.decode("utf-8"))
                    response = broker._dispatch(request)
                except Exception as error:  # final bridge boundary: return actionable error
                    response = {
                        "ok": False, "error_type": type(error).__name__,
                        "error": str(error),
                    }
                self.wfile.write((json.dumps(
                    response, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")) + "\n").encode("utf-8"))
                self.wfile.flush()

        server = _UnixBrokerServer(socket_address, Handler)
        if directory is not None:
            os.chmod(socket_address, 0o666)
        thread = threading.Thread(
            target=server.serve_forever, name="runtime-mcp-broker", daemon=True)
        thread.start()
        self._directory, self._socket_path = directory, public_address
        self._server, self._thread = server, thread
        return self

    def grant(self, *, cycle_id: str, stage: str, target_id: Optional[str],
              purpose: str, ttl_s: Optional[float] = 86400.0,
              pack_hash: str = "", refs=(), workspace_root=None,
              output_uid: Optional[int] = None,
              runner_call_id: Optional[int] = None,
              native_review_ledger: Optional[NativeReviewLedger] = None,
              ) -> str:  # noqa: ANN001
        if self._server is None:
            raise RuntimeError("runtime MCP broker 未启动")
        if stage not in {"idea", "plan", "bundle", "reasoning"}:
            raise ValueError("runtime MCP grant stage 非法")
        if not isinstance(purpose, str) or not purpose:
            raise ValueError("runtime MCP grant purpose 须为非空字符串")
        if not isinstance(pack_hash, str):
            raise ValueError("runtime MCP grant pack_hash 须为字符串")
        if not isinstance(refs, (list, tuple)) or any(
                not isinstance(ref, str) for ref in refs):
            raise ValueError("runtime MCP grant refs 须为字符串数组")
        if workspace_root is not None:
            workspace_root = str(Path(workspace_root).resolve(strict=True))
        if output_uid is not None and (
                isinstance(output_uid, bool) or not isinstance(output_uid, int)
                or output_uid < 0):
            raise ValueError("runtime MCP grant output_uid 非法")
        if runner_call_id is not None and (
                isinstance(runner_call_id, bool)
                or not isinstance(runner_call_id, int) or runner_call_id <= 0):
            raise ValueError("runtime MCP grant runner_call_id 非法")
        if native_review_ledger is not None and not isinstance(
                native_review_ledger, NativeReviewLedger):
            raise ValueError("runtime MCP grant native_review_ledger 非法")
        if native_review_ledger is not None and runner_call_id is None:
            raise ValueError(
                "runtime MCP grant live ledger 必须绑定 runner_call_id")
        if (ttl_s is not None and (
                isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float))
                or not math.isfinite(float(ttl_s)) or float(ttl_s) <= 0)):
            raise ValueError("runtime MCP grant ttl_s 须为正有限数或 None")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._grants[token] = RuntimeMCPScope(
                cycle_id=cycle_id, stage=stage, target_id=target_id,
                purpose=purpose, expires_at=(
                    None if ttl_s is None else time.monotonic() + float(ttl_s)),
                pack_hash=pack_hash, refs=tuple(refs),
                workspace_root=workspace_root, output_uid=output_uid,
                runner_call_id=runner_call_id,
                native_review_ledger=native_review_ledger)
            self._submit_sequences[token] = 0
            self._bundle_cycle_complete.discard(token)
        return token

    def revoke(self, token: Optional[str]) -> None:
        if token is None:
            return
        with self._lock:
            self._grants.pop(token, None)
            self._stage_submissions.pop(token, None)
            self._submit_sequences.pop(token, None)
            self._bundle_cycle_complete.discard(token)

    def latest_stage_submission(self, token: str) -> Optional[Dict[str, Any]]:
        """Return the latest successful submission made with this live token."""
        with self._lock:
            # A submission that crossed the fenced DB commit is already
            # linearized. Capability TTL governs admission, not whether the
            # owner may collect that committed receipt a few microseconds
            # later while unwinding the provider process.
            identity = self._stage_submissions.get(token)
            if identity is not None:
                pass
            else:
                scope = self._grants.get(token)
                if scope is None:
                    raise RuntimeMCPError("runtime MCP capability 不存在或已撤销")
                if (scope.expires_at is not None
                        and time.monotonic() >= scope.expires_at):
                    self._grants.pop(token, None)
                    self._submit_sequences.pop(token, None)
                    self._bundle_cycle_complete.discard(token)
                    raise RuntimeMCPError("runtime MCP capability 已过期")
        if identity is None:
            return None
        return self.service.load_stage_submission(identity[0], identity[1])

    def assert_stage_turn_complete(self, token: str) -> None:
        """Fail a nominal resident turn that did not close its Bundle protocol."""
        with self._lock:
            scope = self._grants.get(token)
            if scope is None:
                raise RuntimeMCPError(
                    "runtime MCP capability 不存在或已撤销")
            if (scope.expires_at is not None
                    and time.monotonic() >= scope.expires_at):
                self._grants.pop(token, None)
                self._stage_submissions.pop(token, None)
                self._submit_sequences.pop(token, None)
                self._bundle_cycle_complete.discard(token)
                raise RuntimeMCPError("runtime MCP capability 已过期")
        self.service.assert_stage_turn_complete(scope)
        if scope.stage != "bundle":
            return
        if self.service._bundle_role(scope) != "legacy":
            return
        with self._lock:
            if self._grants.get(token) is not scope:
                raise RuntimeMCPError(
                    "runtime MCP capability 在退出复验时已撤销")
            if token not in self._bundle_cycle_complete:
                raise RuntimeMCPError(
                    "Bundle 主 turn 尚未观察到 bundle_next_target "
                    "返回 cycle_complete=true")

    def _dispatch(self, request: Any) -> Dict[str, Any]:
        if not isinstance(request, dict):
            raise RuntimeMCPError("broker request 须为 object")
        token = request.get("token")
        if not isinstance(token, str):
            raise RuntimeMCPError("runtime MCP capability token 缺失")
        operation = request.get("operation")
        if operation is not None and operation != "tools/list":
            raise RuntimeMCPError("未知 runtime MCP broker operation")
        tool_name = str(request.get("tool") or "")
        submission_sequence: Optional[int] = None
        needs_commit_fence = tool_name in {
            "wildidea_expand", "wildidea_audit",
            "submit_stage_artifact", "prepare_review", "record_review"}
        with self._lock:
            scope = self._grants.get(token)
            if scope is None:
                raise RuntimeMCPError(
                    "runtime MCP capability 不存在或已撤销")
            if (scope.expires_at is not None
                    and time.monotonic() >= scope.expires_at):
                self._grants.pop(token, None)
                self._stage_submissions.pop(token, None)
                self._submit_sequences.pop(token, None)
                self._bundle_cycle_complete.discard(token)
                raise RuntimeMCPError("runtime MCP capability 已过期")
            if operation == "tools/list":
                if set(request) != {"token", "operation"}:
                    raise RuntimeMCPError(
                        "runtime MCP tools/list request 结构非法")
                return {
                    "ok": True,
                    "tools": _tool_definitions_for_scope(scope),
                }
            if tool_name == "submit_stage_artifact":
                # Sequence is assigned at request admission, not completion.
                # Therefore a later rejected request still invalidates an older
                # accepted draft, while an older slow success can never publish
                # over the later request.
                submission_sequence = self._submit_sequences[token] + 1
                self._submit_sequences[token] = submission_sequence
                self._stage_submissions.pop(token, None)

        @contextmanager
        def submission_commit_fence():
            with self._lock:
                def assert_authorized() -> None:
                    current = self._grants.get(token)
                    if current is not scope:
                        raise RuntimeMCPError(
                            "runtime MCP 写入落库前 capability 已撤销")
                    if (scope.expires_at is not None
                            and time.monotonic() >= scope.expires_at):
                        self._grants.pop(token, None)
                        self._stage_submissions.pop(token, None)
                        self._submit_sequences.pop(token, None)
                        self._bundle_cycle_complete.discard(token)
                        raise RuntimeMCPError(
                            "runtime MCP 写入落库前 capability 已过期")

                assert_authorized()
                yield assert_authorized

        response = self.service.call(
            scope, tool_name, request.get("arguments") or {},
            submission_commit_fence=(
                submission_commit_fence
                if needs_commit_fence else None))
        if (tool_name == "bundle_next_target"
                and response.get("ok") is True):
            with self._lock:
                if self._grants.get(token) is scope:
                    if response.get("cycle_complete") is True:
                        self._bundle_cycle_complete.add(token)
                    else:
                        self._bundle_cycle_complete.discard(token)
        if (tool_name == "bundle_next_target" and response.get("ok") is True
                and response.get("cycle_complete") is False):
            # Binding a new target invalidates the previous target's latest
            # submission for Runner collection.  Incrementing the sequence also
            # fences a concurrently finishing older submit from publishing back
            # over this binding transition.
            with self._lock:
                if self._grants.get(token) is scope:
                    self._submit_sequences[token] = (
                        self._submit_sequences.get(token, 0) + 1)
                    self._stage_submissions.pop(token, None)
        if tool_name == "submit_stage_artifact" and response.get("ok") is True:
            submission_ref = response.get("submission_ref")
            submission_hash = response.get("submission_hash")
            if not isinstance(submission_ref, str) or not isinstance(submission_hash, str):
                raise RuntimeMCPError("阶段提交成功响应缺 file-manager 回执身份")
            with self._lock:
                current = self._grants.get(token)
                # Do not turn an already committed success into an error merely
                # because TTL crossed after COMMIT. The commit fence performed
                # the last authorization check while holding this same lock.
                # A later admitted request still wins the sequence race.
                if (current is scope
                        and self._submit_sequences.get(token) == submission_sequence):
                    self._stage_submissions[token] = (
                        submission_ref, submission_hash)
        return response

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        with self._lock:
            self._grants.clear()
            self._stage_submissions.clear()
            self._submit_sequences.clear()
            self._bundle_cycle_complete.clear()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
        self._directory = None
        self._socket_path = None


def _broker_exchange(request: Mapping[str, Any]) -> Dict[str, Any]:
    path = os.environ.get("METARESEARCH_RUNTIME_MCP_SOCKET")
    token = os.environ.get("METARESEARCH_RUNTIME_MCP_TOKEN")
    if not path or not token:
        raise RuntimeError("runtime MCP bridge 缺 socket/token")
    payload = json.dumps(
        {**dict(request), "token": token},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(payload) > _MAX_MESSAGE_BYTES:
        raise RuntimeError("runtime MCP request 超过 1 MiB")
    socket_address = ("\0" + path[1:]) if path.startswith("@") else path
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        try:
            bridge_timeout = max(
                65.0, float(os.environ.get(
                    "METARESEARCH_RUNNER_TIMEOUT_S", "3600")) - 30.0)
        except ValueError:
            bridge_timeout = 3570.0
        client.settimeout(bridge_timeout)
        client.connect(socket_address)
        client.sendall(payload)
        chunks = bytearray()
        while len(chunks) <= _MAX_MESSAGE_BYTES:
            block = client.recv(min(65536, _MAX_MESSAGE_BYTES + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
            if b"\n" in block:
                break
    if len(chunks) > _MAX_MESSAGE_BYTES:
        raise RuntimeError("runtime MCP response 超过 1 MiB")
    line = bytes(chunks).split(b"\n", 1)[0]
    response = json.loads(line.decode("utf-8"))
    if not isinstance(response, dict):
        raise RuntimeError("runtime MCP broker response 非 object")
    return response


def _broker_request(tool: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    return _broker_exchange({
        "tool": tool,
        "arguments": dict(arguments),
    })


def _broker_tool_definitions() -> list[Dict[str, Any]]:
    response = _broker_exchange({"operation": "tools/list"})
    tools = response.get("tools")
    if response.get("ok") is not True or not isinstance(tools, list):
        raise RuntimeError(
            str(response.get("error") or "runtime MCP tools/list 失败"))
    return tools


def _write_rpc(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True,
        separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _stdio_bridge() -> int:
    """Minimal MCP stdio server; authoritative work stays in the owner broker."""
    instructions = (
        "Quest-scoped persistence tools. Use them from the main stage Codex so "
        "SQL/identity errors are returned in this turn. A clean review child "
        "must call read_review_input with its prepared review_request_id before "
        "returning native-review-result-v1; the resident main agent supplies "
        "dispositions and the revised candidate to record_review. "
        "Question lifecycle and legal baseline publication retain core checks."
    )
    for raw in sys.stdin.buffer:
        if len(raw) > _MAX_MESSAGE_BYTES:
            return 2
        try:
            message = json.loads(raw.decode("utf-8"))
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            method = message.get("method")
            if request_id is None:  # notification
                continue
            if method == "initialize":
                requested = (message.get("params") or {}).get("protocolVersion")
                _write_rpc({
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {
                        "protocolVersion": requested or _PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "meta-research-runtime", "version": "0.1.0"},
                        "instructions": instructions,
                    },
                })
            elif method == "ping":
                _write_rpc({"jsonrpc": "2.0", "id": request_id, "result": {}})
            elif method == "tools/list":
                _write_rpc({
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {"tools": _broker_tool_definitions()},
                })
            elif method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                response = _broker_request(str(name or ""), arguments)
                text = json.dumps(
                    response, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"))
                _write_rpc({
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": response,
                        "isError": response.get("ok") is not True,
                    },
                })
            else:
                _write_rpc({
                    "jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })
        except Exception as error:
            try:
                request_id = message.get("id") if isinstance(message, dict) else None
            except Exception:
                request_id = None
            if request_id is not None:
                _write_rpc({
                    "jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32000, "message": str(error)},
                })
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio-bridge", action="store_true")
    args = parser.parse_args(argv)
    if args.stdio_bridge:
        return _stdio_bridge()
    parser.error("only --stdio-bridge is supported when executed directly")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RuntimeIngestService", "RuntimeMCPBroker", "RuntimeMCPError",
    "RuntimeMCPScope", "main",
]
