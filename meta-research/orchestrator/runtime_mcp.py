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


_MAX_MESSAGE_BYTES = 1024 * 1024
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROTOCOL_VERSION = "2025-06-18"


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
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
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
                "Idea-only deterministic WildIdea expansion capability. Use "
                "inside the resident Idea main turn; it samples the pinned nine "
                "slots without starting another top-level model session."
            ),
            "inputSchema": {
                "type": "object", "required": ["need_innovation"],
                "additionalProperties": False,
                "properties": {"need_innovation": {"type": "boolean"}},
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
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
            "name": "bundle_next_target",
            "description": (
                "Bind and return the next authoritative target inside the one "
                "cycle-wide resident Bundle turn. Call before authoring each "
                "target. It returns the frozen target ContextPack projection, "
                "or cycle_complete=true after every target is terminal."
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
                "Asynchronously start official smoke/train/eval for the target "
                "currently bound by bundle_next_target. Pass the exact receipt "
                "returned by submit_stage_artifact. This call returns promptly; "
                "poll bundle_status for partial logs, terminal state or repair "
                "feedback, then repair or bind the next target in the same turn."
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
                "Poll the currently bound target without blocking. Returns "
                "authoritative state, worker activity, partial/live log tails "
                "and latest repair feedback."
            ),
            "inputSchema": {
                "type": "object", "additionalProperties": False,
                "properties": {},
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
            "name": "record_review",
            "description": (
                "Persist the independent subagent review used by the current main "
                "Codex stage. Call it after the child returns and before the main "
                "agent emits its revised final artifact."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["review_kind", "verdict", "summary_md", "issues"],
                "additionalProperties": False,
                "properties": {
                    "review_kind": {
                        "enum": ["idea", "plan", "bundle_code", "bundle_result"]
                    },
                    "verdict": {"enum": ["pass", "fail"]},
                    "summary_md": {"type": "string", "maxLength": 65536},
                    "issues": {
                        "type": "array", "maxItems": 64,
                        "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    },
                    "subject_hash": {
                        "type": ["string", "null"],
                        "pattern": "^(sha256:)?[0-9a-f]{64}$",
                    },
                },
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
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
        """Bind the owner-side official Bundle pipeline after assembly."""
        if controller is None or not all(callable(getattr(controller, name, None)) for name in (
                "bind_next_bundle_target", "bundle_session_scope",
                "execute_bundle_session", "bundle_session_status",
                "request_bundle_repair", "replan_bundle_session")):
            raise ValueError(
                "bundle controller 缺 next/scope/execute/status/replan capability")
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
            "bundle_next_target": self._bundle_next_target,
            "bundle_execute": self._bundle_execute,
            "bundle_status": self._bundle_status,
            "bundle_repair": self._bundle_repair,
            "bundle_replan": self._bundle_replan,
            "upsert_card": self._upsert_card,
            "record_review": self._record_review,
            "record_cycle_summary": self._record_cycle_summary,
            "submit_stage_artifact": self._submit_stage_artifact,
        }
        handler = handlers.get(name)
        if handler is None:
            raise RuntimeMCPError(f"未知 runtime MCP tool: {name!r}")
        if (scope.stage == "bundle" and name in {
                "submit_stage_artifact", "record_review", "bundle_execute",
                "bundle_status", "bundle_repair", "bundle_replan"}):
            scope = self._effective_bundle_scope(scope)
        if name == "submit_stage_artifact":
            return handler(
                scope, dict(arguments), commit_fence=submission_commit_fence)
        return handler(scope, dict(arguments))

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
        result = controller.execute_bundle_session(scope, loaded["files"])
        if not isinstance(result, dict):
            raise RuntimeMCPError("Bundle controller 返回非 object")
        return {"ok": True, **result}

    def _bundle_status(self, scope: RuntimeMCPScope,
                       arguments: Dict[str, Any]) -> Dict[str, Any]:
        if arguments:
            raise RuntimeMCPError("bundle_status 不接受参数")
        controller = self._require_bundle_controller(scope)
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

    def _wildidea_expand(self, scope: RuntimeMCPScope,
                         arguments: Dict[str, Any]) -> Dict[str, Any]:
        if scope.stage != "idea":
            raise RuntimeMCPError("wildidea_expand 只允许 Idea 主阶段调用")
        if set(arguments) != {"need_innovation"} or not isinstance(
                arguments.get("need_innovation"), bool):
            raise RuntimeMCPError("wildidea_expand 须且只须提供 need_innovation boolean")
        if self.wildidea_adapter is None:
            raise RuntimeMCPError("当前运行时未装配 WildIdea capability")
        try:
            result = self.wildidea_adapter.expand_for_tool(
                pack_hash=scope.pack_hash,
                need_innovation=arguments["need_innovation"])
        except Exception as error:
            raise RuntimeMCPError(f"WildIdea expansion 失败: {error}") from error
        return {"ok": True, **result}

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

    def _record_review(self, scope: RuntimeMCPScope,
                       arguments: Dict[str, Any]) -> Dict[str, Any]:
        goal_id, goal_ver, active_q, _status = self._cycle_row(scope)
        ci = _cycle_number(scope.cycle_id)
        review_kind = arguments.get("review_kind")
        allowed_by_stage = {
            "idea": {"idea"}, "plan": {"plan"},
            "bundle": {"bundle_code", "bundle_result"},
        }
        if review_kind not in allowed_by_stage.get(scope.stage, set()):
            raise RuntimeMCPError(
                f"review_kind={review_kind!r} 不属于当前 stage={scope.stage!r}")
        verdict = arguments.get("verdict")
        if verdict not in {"pass", "fail"}:
            raise RuntimeMCPError("review verdict 非法")
        summary = _bounded_text(
            arguments.get("summary_md"), name="summary_md", maximum=65536,
            allow_empty=True)
        issues = arguments.get("issues")
        if (not isinstance(issues, list) or len(issues) > 64
                or any(not isinstance(item, str) or not item.strip()
                       or len(item.encode("utf-8")) > 4096 for item in issues)):
            raise RuntimeMCPError("review issues 须为最多 64 条非空短文本")
        subject_hash = arguments.get("subject_hash")
        if subject_hash is not None and re.fullmatch(
                r"(?:sha256:)?[0-9a-f]{64}", str(subject_hash)) is None:
            raise RuntimeMCPError("subject_hash 非法")
        if subject_hash is not None and not str(subject_hash).startswith("sha256:"):
            subject_hash = "sha256:" + str(subject_hash)
        payload = {
            "protocol": "codex-subagent-review-v1",
            "stage": scope.stage, "target_id": scope.target_id,
            "purpose": scope.purpose, "review_kind": review_kind,
            "verdict": verdict, "summary_md": summary,
            "issues": list(issues), "subject_hash": subject_hash,
            "goal_id": goal_id, "goal_ver": goal_ver,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.daemon.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM decision WHERE cycle_id=? AND actor='agent' "
                "AND type='runtime_review' AND payload_json=? ORDER BY id LIMIT 1",
                (ci, encoded),
            ).fetchone()
            if existing is not None:
                return {"ok": True, "created": False,
                        "decision_id": int(existing[0]), **payload}
            decision_id = conn.execute(
                "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
                "VALUES (?,?,'agent','runtime_review',?)",
                (ci, active_q, encoded),
            ).lastrowid
        return {"ok": True, "created": True,
                "decision_id": int(decision_id), **payload}

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

    def _required_review_in_txn(
            self, conn: sqlite3.Connection, scope: RuntimeMCPScope,
            submission_kind: str) -> Optional[int]:
        """Require the configured one child-review receipt before submission.

        The resident main agent remains responsible for spawning the clean
        child and applying its findings.  This small gate proves that the live
        turn recorded the configured review for this exact stage/target and
        purpose; it does not launch or interpret another model call.
        """
        review_kind_by_submission = {
            "idea": "idea", "plan": "plan", "bundle": "bundle_code",
        }
        policy_key_by_review = {
            "idea": "plan_review", "plan": "plan_review",
            "bundle_code": "bundle_code_review",
        }
        review_kind = review_kind_by_submission.get(submission_kind)
        if review_kind is None:
            return None
        retry = (((self.policy or {}).get("flow") or {}).get("retry") or {})
        rounds = retry.get(policy_key_by_review[review_kind], 0)
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
            raise RuntimeMCPError("runtime review 配置非法")
        if rounds == 0:
            return None
        ci = _cycle_number(scope.cycle_id)
        row = conn.execute(
            "SELECT id FROM decision WHERE cycle_id=? AND actor='agent' "
            "AND type='runtime_review' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.protocol')='codex-subagent-review-v1' "
            "AND json_extract(payload_json,'$.stage')=? "
            "AND coalesce(json_extract(payload_json,'$.target_id'),'')=? "
            "AND json_extract(payload_json,'$.purpose')=? "
            "AND json_extract(payload_json,'$.review_kind')=? "
            "AND json_extract(payload_json,'$.verdict') IN ('pass','fail') "
            "ORDER BY id DESC LIMIT 1",
            (ci, scope.stage, scope.target_id or "", scope.purpose, review_kind),
        ).fetchone()
        if row is None:
            raise RuntimeMCPError(
                "最终阶段提交前缺当前主 turn 的独立子智能体评审记录；"
                f"请先调用 record_review(review_kind={review_kind})，吸收意见后在同一 turn 重提")
        return int(row[0])

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
                        conn, scope, submission_kind)
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
        root_hint = str(self.runtime_root) if self.runtime_root is not None else ""
        use_abstract = (
            sys.platform.startswith("linux")
            and self.runtime_root is not None
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
              output_uid: Optional[int] = None) -> str:  # noqa: ANN001
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
                workspace_root=workspace_root, output_uid=output_uid)
            self._submit_sequences[token] = 0
        return token

    def revoke(self, token: Optional[str]) -> None:
        if token is None:
            return
        with self._lock:
            self._grants.pop(token, None)
            self._stage_submissions.pop(token, None)
            self._submit_sequences.pop(token, None)

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
                    raise RuntimeMCPError("runtime MCP capability 已过期")
        if identity is None:
            return None
        return self.service.load_stage_submission(identity[0], identity[1])

    def _dispatch(self, request: Any) -> Dict[str, Any]:
        if not isinstance(request, dict):
            raise RuntimeMCPError("broker request 须为 object")
        token = request.get("token")
        if not isinstance(token, str):
            raise RuntimeMCPError("runtime MCP capability token 缺失")
        tool_name = str(request.get("tool") or "")
        submission_sequence: Optional[int] = None
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
                raise RuntimeMCPError("runtime MCP capability 已过期")
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
                            "阶段提交落库前 capability 已撤销")
                    if (scope.expires_at is not None
                            and time.monotonic() >= scope.expires_at):
                        self._grants.pop(token, None)
                        self._stage_submissions.pop(token, None)
                        self._submit_sequences.pop(token, None)
                        raise RuntimeMCPError(
                            "阶段提交落库前 capability 已过期")

                assert_authorized()
                yield assert_authorized

        response = self.service.call(
            scope, tool_name, request.get("arguments") or {},
            submission_commit_fence=(
                submission_commit_fence
                if submission_sequence is not None else None))
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
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
        self._directory = None
        self._socket_path = None


def _broker_request(tool: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    path = os.environ.get("METARESEARCH_RUNTIME_MCP_SOCKET")
    token = os.environ.get("METARESEARCH_RUNTIME_MCP_TOKEN")
    if not path or not token:
        raise RuntimeError("runtime MCP bridge 缺 socket/token")
    payload = json.dumps({
        "token": token, "tool": tool, "arguments": dict(arguments),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
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


def _write_rpc(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True,
        separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _stdio_bridge() -> int:
    """Minimal MCP stdio server; authoritative work stays in the owner broker."""
    tools = _tool_definitions()
    instructions = (
        "Quest-scoped persistence tools. Use them from the main stage Codex so "
        "SQL/identity errors are returned in this turn. Review subagents should "
        "return findings to the main agent; the main agent calls record_review. "
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
                    "result": {"tools": tools},
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
