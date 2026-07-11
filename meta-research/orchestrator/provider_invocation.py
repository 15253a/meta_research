"""Durable provider invocation and usage receipts.

An execution guardian receipt proves only the process-tree outcome.  This
receipt binds the provider-visible invocation identity and observed token usage
to the already-durable ``runner_call`` intent and to that exact guardian
operation.  Monetary values deliberately do not live here: ``CostLedger``
records the local policy projection in the same transaction as the ledger row,
without pretending that it is a supplier invoice.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from .interfaces import CallUsage
from .process_supervisor import (atomic_write_receipt, read_execution_capture,
                                 read_receipt, validate_execution_receipt)


PROTOCOL = "provider-invocation-v1"
VERSION = 1
RUNNER_RECONCILE_PROTOCOL = "runner-call-v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPERATION_RE = re.compile(r"^exec-[0-9a-f]{32}$")
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RECEIPT_NAME_RE = re.compile(r"^provider-invocation-rc([1-9][0-9]*)\.json$")
_OUTCOMES = frozenset({
    "exit", "timeout", "cancelled", "owner_lost", "spawn_failed",
    "lingering_descendant", "owner_lost_before_start",
})
_USAGE_SOURCES = frozenset({
    "stderr_tokens_used", "json_turn_completed", "stderr_and_json",
    "unavailable", "conflict",
})
_TOP_KEYS = frozenset({
    "protocol", "version", "runner_call_id", "cycle_id", "phase", "purpose",
    "provider", "model", "effort", "prompt_sha256", "local_invocation_id",
    "provider_invocation_id", "provider_invocation_id_kind", "usage_source",
    "usage", "execution",
})
_USAGE_KEYS = frozenset({
    "tokens_known", "tokens_input", "tokens_output", "tokens_total",
    "wallclock_sec",
})
_EXECUTION_KEYS = frozenset({
    "receipt_ref", "receipt_sha256", "operation_id", "outcome", "returncode",
})


class ProviderInvocationError(RuntimeError):
    """A provider receipt is missing, ambiguous, or inconsistent with authority."""


@dataclass(frozen=True)
class ProviderInvocation:
    receipt_ref: str
    receipt_sha256: str
    runner_call_id: int
    cycle_id: str
    phase: str
    purpose: str
    provider: str
    model: str
    effort: str
    prompt_sha256: str
    local_invocation_id: str
    provider_invocation_id: Optional[str]
    provider_invocation_id_kind: Optional[str]
    usage_source: str
    usage: CallUsage
    execution_receipt_ref: str
    execution_receipt_sha256: str
    execution_operation_id: str
    execution_outcome: str
    execution_returncode: Optional[int]


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False) + "\n").encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _nonempty(value: Any, *, field: str, max_len: int = 256) -> str:
    if (not isinstance(value, str) or not value.strip()
            or len(value) > max_len or "\x00" in value):
        raise ProviderInvocationError(f"{field} 须为有界非空字符串")
    return value


def _usage_payload(usage: Optional[CallUsage]) -> Dict[str, Any]:
    value = usage if usage is not None else CallUsage(tokens_known=False)
    known = getattr(value, "tokens_known", None)
    if not isinstance(known, bool):
        raise ProviderInvocationError("usage.tokens_known 须为 bool")
    tokens: Dict[str, int] = {}
    for field in ("tokens_input", "tokens_output", "tokens_total"):
        item = getattr(value, field, None)
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item < (1 << 63):
            raise ProviderInvocationError(f"usage.{field} 非法")
        tokens[field] = item
    if not known and any(tokens.values()):
        raise ProviderInvocationError("未知 token usage 不得携非零 token")
    if ((tokens["tokens_input"] or tokens["tokens_output"])
            and tokens["tokens_total"] < tokens["tokens_input"] + tokens["tokens_output"]):
        raise ProviderInvocationError("usage.tokens_total 小于 input+output")
    wallclock = getattr(value, "wallclock_sec", None)
    if isinstance(wallclock, bool) or not isinstance(wallclock, (int, float)):
        raise ProviderInvocationError("usage.wallclock_sec 非法")
    wallclock = float(wallclock)
    if not math.isfinite(wallclock) or wallclock < 0:
        raise ProviderInvocationError("usage.wallclock_sec 须为有限非负数")
    return {"tokens_known": known, **tokens, "wallclock_sec": wallclock}


def _usage_from_payload(value: Any) -> CallUsage:
    if not isinstance(value, dict) or set(value) != _USAGE_KEYS:
        raise ProviderInvocationError("provider receipt usage 字段集合非法")
    payload = _usage_payload(CallUsage(
        tokens_known=value.get("tokens_known"),
        tokens_input=value.get("tokens_input"),
        tokens_output=value.get("tokens_output"),
        tokens_total=value.get("tokens_total"),
        wallclock_sec=value.get("wallclock_sec"),
    ))
    return CallUsage(**payload)


def provider_receipt_path(receipt_dir: Path, runner_call_id: int) -> Path:
    if (isinstance(runner_call_id, bool) or not isinstance(runner_call_id, int)
            or runner_call_id <= 0):
        raise ProviderInvocationError("runner_call_id 须为正整数")
    return Path(receipt_dir) / f"provider-invocation-rc{runner_call_id}.json"


def provider_receipt_for_execution(execution_receipt_ref: str,
                                   runner_call_id: int) -> Path:
    execution_path = Path(os.path.abspath(os.fspath(execution_receipt_ref)))
    return provider_receipt_path(execution_path.parent, runner_call_id)


def receipt_runner_call_id(path: Path) -> int:
    match = _RECEIPT_NAME_RE.fullmatch(Path(path).name)
    if match is None:
        raise ProviderInvocationError(f"provider receipt 文件名非法: {Path(path).name}")
    return int(match.group(1))


def _validate_execution_receipt(value: Dict[str, Any], *, runner_call_id: int,
                                cycle_id: str, phase: str, purpose: str,
                                receipt_ref: str) -> Tuple[str, str, str, Optional[int]]:
    validate_execution_receipt(value, Path(receipt_ref))
    context = value.get("context")
    if (value.get("state") != "terminal" or value.get("group_drained") is not True
            or not isinstance(context, dict)):
        raise ProviderInvocationError("execution receipt 未证明 terminal+drained")
    if (context.get("reconcile_protocol") != RUNNER_RECONCILE_PROTOCOL
            or context.get("db_owner_kind") != "runner_call"
            or context.get("db_owner_id") != runner_call_id
            or context.get("cycle_id") != cycle_id
            or context.get("db_phase") != phase
            or context.get("db_purpose") != purpose):
        raise ProviderInvocationError("execution receipt 与 runner_call 身份不一致")
    operation_id = value.get("operation_id")
    outcome = value.get("outcome")
    returncode = value.get("returncode")
    if not isinstance(operation_id, str) or _OPERATION_RE.fullmatch(operation_id) is None:
        raise ProviderInvocationError("execution operation_id 非法")
    if outcome not in _OUTCOMES:
        raise ProviderInvocationError(f"execution outcome 非法: {outcome!r}")
    if returncode is not None and (isinstance(returncode, bool) or not isinstance(returncode, int)):
        raise ProviderInvocationError("execution returncode 非法")
    if outcome == "exit" and returncode is None:
        raise ProviderInvocationError("exit execution receipt 缺 returncode")
    return os.path.abspath(receipt_ref), _digest(value), operation_id, returncode


def write_provider_invocation_receipt(
        *, receipt_dir: Path, runner_call_id: int, cycle_id: str, phase: str,
        purpose: str, provider: str, model: str, effort: str,
        prompt_sha256: str, usage: Optional[CallUsage], usage_source: str,
        execution_receipt_ref: str,
        provider_invocation_id: Optional[str] = None,
        provider_invocation_id_kind: Optional[str] = None) -> str:
    """Publish exactly one receipt after the guarded provider tree is terminal."""
    path = provider_receipt_path(receipt_dir, runner_call_id)
    if path.exists() or path.is_symlink():
        raise ProviderInvocationError(
            f"runner_call {runner_call_id} 已存在 provider invocation receipt")
    cycle_id = _nonempty(cycle_id, field="cycle_id", max_len=64)
    phase = _nonempty(phase, field="phase", max_len=64)
    purpose = _nonempty(purpose, field="purpose", max_len=256)
    provider = _nonempty(provider, field="provider", max_len=64)
    model = _nonempty(model, field="model", max_len=128)
    effort = _nonempty(effort, field="effort", max_len=64)
    if not isinstance(prompt_sha256, str) or _SHA256_RE.fullmatch(prompt_sha256) is None:
        raise ProviderInvocationError("prompt_sha256 非法")
    if usage_source not in _USAGE_SOURCES:
        raise ProviderInvocationError(f"usage_source 非法: {usage_source!r}")
    if provider_invocation_id is None:
        if provider_invocation_id_kind is not None:
            raise ProviderInvocationError("provider invocation id 缺失时 kind 也须为空")
    else:
        if (_PROVIDER_ID_RE.fullmatch(provider_invocation_id) is None
                or provider_invocation_id_kind not in ("session_id", "thread_id")):
            raise ProviderInvocationError("provider invocation id/kind 非法")

    execution_ref = os.path.abspath(os.fspath(execution_receipt_ref))
    execution_value = read_receipt(Path(execution_ref))
    execution_ref, execution_hash, operation_id, returncode = _validate_execution_receipt(
        execution_value, runner_call_id=runner_call_id, cycle_id=cycle_id,
        phase=phase, purpose=purpose, receipt_ref=execution_ref)
    value = {
        "protocol": PROTOCOL,
        "version": VERSION,
        "runner_call_id": runner_call_id,
        "cycle_id": cycle_id,
        "phase": phase,
        "purpose": purpose,
        "provider": provider,
        "model": model,
        "effort": effort,
        "prompt_sha256": prompt_sha256,
        "local_invocation_id": operation_id,
        "provider_invocation_id": provider_invocation_id,
        "provider_invocation_id_kind": provider_invocation_id_kind,
        "usage_source": usage_source,
        "usage": _usage_payload(usage),
        "execution": {
            "receipt_ref": execution_ref,
            "receipt_sha256": execution_hash,
            "operation_id": operation_id,
            "outcome": execution_value["outcome"],
            "returncode": returncode,
        },
    }
    atomic_write_receipt(path, value)
    return str(path)


def load_provider_invocation_receipt(
        path: Path, *, expected_runner_call_id: int, expected_cycle_id: str,
        expected_phase: str, expected_purpose: str,
        expected_execution_receipt_ref: Optional[str] = None) -> ProviderInvocation:
    """Strictly validate a receipt against DB-derived runner authority."""
    path = Path(os.path.abspath(os.fspath(path)))
    if receipt_runner_call_id(path) != expected_runner_call_id:
        raise ProviderInvocationError("provider receipt 文件名与 runner_call_id 不一致")
    value = read_receipt(path)
    if set(value) != _TOP_KEYS:
        raise ProviderInvocationError("provider receipt 顶层字段集合非法")
    if value.get("protocol") != PROTOCOL or value.get("version") != VERSION:
        raise ProviderInvocationError("provider receipt protocol/version 非法")
    if (value.get("runner_call_id") != expected_runner_call_id
            or value.get("cycle_id") != expected_cycle_id
            or value.get("phase") != expected_phase
            or value.get("purpose") != expected_purpose):
        raise ProviderInvocationError("provider receipt 与 DB runner_call 身份不一致")
    for field, limit in (("provider", 64), ("model", 128), ("effort", 64)):
        _nonempty(value.get(field), field=field, max_len=limit)
    prompt_hash = value.get("prompt_sha256")
    if not isinstance(prompt_hash, str) or _SHA256_RE.fullmatch(prompt_hash) is None:
        raise ProviderInvocationError("provider receipt prompt_sha256 非法")
    usage_source = value.get("usage_source")
    if usage_source not in _USAGE_SOURCES:
        raise ProviderInvocationError("provider receipt usage_source 非法")
    usage = _usage_from_payload(value.get("usage"))
    provider_id = value.get("provider_invocation_id")
    provider_id_kind = value.get("provider_invocation_id_kind")
    if provider_id is None:
        if provider_id_kind is not None:
            raise ProviderInvocationError("provider receipt id/kind 不成对")
    elif (_PROVIDER_ID_RE.fullmatch(provider_id) is None
          or provider_id_kind not in ("session_id", "thread_id")):
        raise ProviderInvocationError("provider receipt id/kind 非法")

    execution = value.get("execution")
    if not isinstance(execution, dict) or set(execution) != _EXECUTION_KEYS:
        raise ProviderInvocationError("provider receipt execution 字段集合非法")
    execution_ref = execution.get("receipt_ref")
    if not isinstance(execution_ref, str) or not execution_ref:
        raise ProviderInvocationError("provider receipt execution ref 非法")
    execution_ref = os.path.abspath(execution_ref)
    if (expected_execution_receipt_ref is not None
            and execution_ref != os.path.abspath(expected_execution_receipt_ref)):
        raise ProviderInvocationError("provider receipt 指向非预期 execution receipt")
    if path != provider_receipt_for_execution(execution_ref, expected_runner_call_id):
        raise ProviderInvocationError("provider receipt 未与 execution receipt 共址/确定性命名")
    execution_value = read_receipt(Path(execution_ref))
    checked_ref, execution_hash, operation_id, returncode = _validate_execution_receipt(
        execution_value, runner_call_id=expected_runner_call_id,
        cycle_id=expected_cycle_id, phase=expected_phase,
        purpose=expected_purpose, receipt_ref=execution_ref)
    if (execution.get("receipt_sha256") != execution_hash
            or execution.get("operation_id") != operation_id
            or execution.get("outcome") != execution_value.get("outcome")
            or execution.get("returncode") != returncode
            or value.get("local_invocation_id") != operation_id):
        raise ProviderInvocationError("provider receipt 与 execution receipt 内容锚不一致")
    receipt_hash = _digest(value)
    return ProviderInvocation(
        receipt_ref=str(path), receipt_sha256=receipt_hash,
        runner_call_id=expected_runner_call_id,
        cycle_id=expected_cycle_id, phase=expected_phase,
        purpose=expected_purpose, provider=value["provider"], model=value["model"],
        effort=value["effort"], prompt_sha256=prompt_hash,
        local_invocation_id=operation_id,
        provider_invocation_id=provider_id,
        provider_invocation_id_kind=provider_id_kind,
        usage_source=usage_source, usage=usage,
        execution_receipt_ref=checked_ref,
        execution_receipt_sha256=execution_hash,
        execution_operation_id=operation_id,
        execution_outcome=execution_value["outcome"],
        execution_returncode=returncode,
    )


def reconstruct_provider_invocation_receipt(
        execution_receipt_path: Path, *, expected_runner_call_id: int,
        expected_cycle_id: str, expected_phase: str,
        expected_purpose: str) -> ProviderInvocation:
    """Rebuild the provider fact from guardian-owned captures after owner death.

    The local import avoids a module cycle: ``runner`` uses this receipt module
    on the normal path, while startup recovery reuses runner's exact parsers.
    """
    execution_path = Path(os.path.abspath(os.fspath(execution_receipt_path)))
    execution = read_receipt(execution_path)
    _validate_execution_receipt(
        execution, runner_call_id=expected_runner_call_id,
        cycle_id=expected_cycle_id, phase=expected_phase,
        purpose=expected_purpose, receipt_ref=str(execution_path))
    context = execution.get("context") or {}
    provider = context.get("provider")
    model = context.get("provider_model")
    effort = context.get("provider_effort")
    prompt_sha256 = context.get("prompt_sha256")
    if provider != "codex-cli":
        raise ProviderInvocationError("execution receipt 不声明可恢复的 codex provider")
    _nonempty(model, field="provider_model", max_len=128)
    _nonempty(effort, field="provider_effort", max_len=64)
    if not isinstance(prompt_sha256, str) or _SHA256_RE.fullmatch(prompt_sha256) is None:
        raise ProviderInvocationError("execution receipt 缺 prompt_sha256")
    if execution.get("capture_stdout_ref") is None:
        raise ProviderInvocationError("execution receipt 缺 guardian durable capture")
    stdout = read_execution_capture(execution, stream="stdout").decode("utf-8", "replace")
    stderr = read_execution_capture(execution, stream="stderr").decode("utf-8", "replace")
    started, finished = execution.get("started_at_unix"), execution.get("finished_at_unix")
    wallclock = 0.0
    if (not isinstance(started, bool) and isinstance(started, (int, float))
            and not isinstance(finished, bool) and isinstance(finished, (int, float))
            and math.isfinite(float(started)) and math.isfinite(float(finished))):
        wallclock = round(max(0.0, float(finished) - float(started)), 3)
    from .runner import CodexRunner, parse_provider_invocation_id  # local: see docstring
    usage, usage_source = CodexRunner._usage_with_source(stderr, wallclock, stdout)
    provider_id, provider_id_kind = parse_provider_invocation_id(stderr, stdout)
    path = write_provider_invocation_receipt(
        receipt_dir=execution_path.parent,
        runner_call_id=expected_runner_call_id,
        cycle_id=expected_cycle_id, phase=expected_phase,
        purpose=expected_purpose, provider=provider,
        model=model, effort=effort, prompt_sha256=prompt_sha256,
        usage=usage, usage_source=usage_source,
        execution_receipt_ref=str(execution_path),
        provider_invocation_id=provider_id,
        provider_invocation_id_kind=provider_id_kind)
    return load_provider_invocation_receipt(
        Path(path), expected_runner_call_id=expected_runner_call_id,
        expected_cycle_id=expected_cycle_id, expected_phase=expected_phase,
        expected_purpose=expected_purpose,
        expected_execution_receipt_ref=str(execution_path))


def recovery_terminal(invocation: ProviderInvocation) -> Tuple[str, str]:
    """Map a process result to an honest runner terminal after owner loss.

    Even drained exit(0) cannot prove that envelope/schema/business validation
    completed, so recovery accounts its usage but never synthesizes success.
    """
    outcome = invocation.execution_outcome
    if outcome == "exit":
        return ("failed", "orphaned_after_provider_receipt"
                if invocation.execution_returncode == 0 else "runtime")
    if outcome == "timeout":
        return "failed", "timeout"
    if outcome == "spawn_failed":
        return "failed", "env_invalid"
    if outcome == "lingering_descendant":
        return "failed", "lingering_descendant"
    if outcome in ("cancelled", "owner_lost", "owner_lost_before_start"):
        return "aborted", outcome
    raise ProviderInvocationError(f"未知 provider execution outcome: {outcome!r}")
