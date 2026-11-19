"""Shared error facts for every public operation boundary.

The Owner remains authoritative for domain state.  This module only gives the
transport and stage adapters one typed projection of a failure, including the
identity needed to reconcile an uncertain effect.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping


class EffectStatus(str, Enum):
    NOT_STARTED = "not_started"
    APPLIED = "applied"
    WAITING = "waiting"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    STALE = "stale"
    CANCELLED = "cancelled"


class RecoveryOwner(str, Enum):
    AGENT = "agent"
    SYSTEM = "system"
    OPERATOR = "operator"


class NextAction(str, Enum):
    CORRECT_INPUT = "correct_input"
    REREAD_CURRENT_STATE = "reread_current_state"
    RETRY_SAFE = "retry_safe"
    WAIT = "wait"
    RECONCILE = "reconcile"
    RESUME = "resume"
    SUCCESSOR = "successor"
    ESCALATE = "escalate"
    CANCEL = "cancel"


_EFFECT_STATUSES = frozenset(item.value for item in EffectStatus)
_ROUTING = {
    "agent": RecoveryOwner.AGENT,
    "system": RecoveryOwner.SYSTEM,
    "operator": RecoveryOwner.OPERATOR,
}
PROVIDER_INSUFFICIENT_BALANCE = "provider_insufficient_balance"


STABLE_ERROR_CODE_MAP = {
    "semantic_input_schema_mismatch": "input.invalid",
    "capability_unavailable": "capability.unavailable",
    "semantic_operation_failed": "operation.unknown",
    "semantic_output_schema_mismatch": "output.invalid",
    "attempt_fence_stale": "fence.stale",
    "target_work_request_invalid": "target.request.rejected",
    "target_work_reconciliation_invalid": "target.reconcile.invalid",
}


def _safe(value: Any, *, depth: int = 0) -> Any:
    """Keep error projections JSON-safe and free of raw exception objects."""

    if depth > 3:
        return "<truncated>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _safe(item, depth=depth + 1)
            for key, item in value.items()
            if isinstance(key, (str, int, float, bool))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(item, depth=depth + 1) for item in value]
    return f"<{type(value).__name__}>"


def _audit_identity(facts: Mapping[str, Any]) -> str:
    body = json.dumps(facts, sort_keys=True, separators=(",", ":"))
    return "error:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def _route_for(code: str, details: Mapping[str, Any]) -> RecoveryOwner:
    if code == "mcp_channel_authentication_required":
        return RecoveryOwner.OPERATOR
    if code == "capability_unavailable" and details.get("reason") in {"not_granted", "not_registered"}:
        return RecoveryOwner.OPERATOR
    if any(token in code for token in ("audit", "persistence", "invariant", "corrupt", "readback")):
        return RecoveryOwner.OPERATOR
    if any(token in code for token in ("schema", "input", "target_", "context", "authorization", "permission", "scope")):
        return RecoveryOwner.AGENT
    if details.get("recovery_owner") in _ROUTING:
        return _ROUTING[str(details["recovery_owner"])]
    return RecoveryOwner.SYSTEM


def _status_for(code: str, details: Mapping[str, Any], *, effect: bool) -> EffectStatus:
    if "fence_stale" in code or code in {"attempt_fence_stale", "stale_fence"}:
        return EffectStatus.STALE
    raw = details.get("effect_status")
    legacy_statuses = {
        "not_applied": EffectStatus.NOT_STARTED,
        "effect_confirmed": EffectStatus.APPLIED,
        "unknown_outcome": EffectStatus.UNKNOWN,
    }
    if isinstance(raw, str) and raw in _EFFECT_STATUSES:
        return EffectStatus(raw)
    if isinstance(raw, str) and raw in legacy_statuses:
        return legacy_statuses[raw]
    if details.get("call_execution") == "not_started":
        return EffectStatus.NOT_STARTED
    if code == "mcp_channel_authentication_required":
        return EffectStatus.REJECTED
    if code == "capability_unavailable" and details.get("reason") in {"not_granted", "not_registered"}:
        return EffectStatus.REJECTED
    if "cancel" in code:
        return EffectStatus.CANCELLED
    if any(token in code for token in ("conflict", "already_exists", "authorization_denied", "permission_denied")):
        return EffectStatus.REJECTED
    if details.get("status") in {"waiting", "waiting_user", "pending"}:
        return EffectStatus.WAITING
    if details.get("status") == "unknown_outcome":
        return EffectStatus.UNKNOWN
    if effect:
        return EffectStatus.UNKNOWN
    return EffectStatus.NOT_STARTED


@dataclass(frozen=True)
class ErrorContract:
    code: str
    phase: str
    operation_id: str
    legacy_code: str | None = None
    operation_ref: str | None = None
    effect_id: str | None = None
    effect_status: EffectStatus = EffectStatus.NOT_STARTED
    field_path: tuple[str | int, ...] = ()
    handoff: Mapping[str, Any] = field(default_factory=dict)
    expected: Any = None
    actual: Any = None
    feedback_stage_agent: bool = False
    correction: str | None = None
    retry_allowed: bool = False
    reconcile_same_effect: bool = False
    next_action: NextAction = NextAction.ESCALATE
    resume_required: bool = False
    successor_required: bool = False
    operator_required: bool = False
    recovery_owner: RecoveryOwner = RecoveryOwner.SYSTEM
    recovery_component: str = "semantic_mcp"
    audit_ref: str | None = None
    correlation_ref: str | None = None
    _generated_audit_ref: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.code or not self.phase or not self.operation_id:
            raise ValueError("error_contract_identity_invalid")
        if not isinstance(self.effect_status, EffectStatus):
            raise ValueError("error_contract_effect_status_invalid")
        if not isinstance(self.recovery_owner, RecoveryOwner):
            raise ValueError("error_contract_recovery_owner_invalid")
        if self.operator_required and self.recovery_owner is not RecoveryOwner.OPERATOR:
            raise ValueError("error_contract_operator_route_invalid")
        if self.effect_status is EffectStatus.UNKNOWN and self.retry_allowed:
            raise ValueError("error_contract_unknown_retry_invalid")
        if self.effect_status is EffectStatus.UNKNOWN and not self.reconcile_same_effect:
            raise ValueError("error_contract_unknown_reconcile_required")
        if self.effect_status is EffectStatus.STALE and self.retry_allowed:
            raise ValueError("error_contract_stale_retry_invalid")

    @property
    def legacy_generated_audit_ref(self) -> str | None:
        if self._generated_audit_ref is None or self.audit_ref != self._generated_audit_ref:
            return None
        return _audit_identity({"code": self.code, "operation_id": self.operation_id, "effect_id": self.effect_id})

    @property
    def category(self) -> str:
        if self.feedback_stage_agent:
            return "agent"
        if self.operator_required:
            return "operator"
        return "system"

    @property
    def stable_code(self) -> str:
        return STABLE_ERROR_CODE_MAP.get(self.code, self.code)

    def as_details(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "stable_code": self.stable_code,
            "legacy_code": self.legacy_code or self.code,
            "phase": self.phase,
            "operation_id": self.operation_id,
            "operation_ref": self.operation_ref,
            "effect_id": self.effect_id,
            "effect_status": self.effect_status.value,
            "field_path": list(self.field_path),
            "handoff": dict(_safe(self.handoff)),
            "expected": _safe(self.expected),
            "actual": _safe(self.actual),
            "feedback_stage_agent": self.feedback_stage_agent,
            "correction": self.correction,
            "retry_allowed": self.retry_allowed,
            "reconcile_same_effect": self.reconcile_same_effect,
            "next_action": self.next_action.value,
            "resume_required": self.resume_required,
            "successor_required": self.successor_required,
            "operator_required": self.operator_required,
            "recovery_owner": self.recovery_owner.value,
            "recovery_component": self.recovery_component,
            "audit_ref": self.audit_ref,
            "correlation_ref": self.correlation_ref,
            "category": self.category,
        }
        return value

    def as_public_dict(self) -> dict[str, Any]:
        return {"code": self.code, "details": self.as_details()}


def error_contract_from_legacy(
    code: str,
    details: Mapping[str, Any] | None = None,
    *,
    phase: str = "handler",
    operation_id: str = "unknown",
    effect: bool = False,
    operation_ref: str | None = None,
    effect_id: str | None = None,
    correlation_ref: str | None = None,
) -> ErrorContract:
    """Map an existing code/details pair without changing Owner semantics."""

    raw = dict(details or {})
    if code == PROVIDER_INSUFFICIENT_BALANCE and type(raw.get("next_retry_at")) in {int, float}:
        raw.setdefault("effect_status", "waiting")
        raw.setdefault("next_action", "wait")
        raw.setdefault("recovery_component", "target_runtime")
        raw.setdefault("handoff", {"next_retry_at": raw["next_retry_at"]})
    operation_id = str(raw.get("operation_id") or operation_id)
    effect_id = effect_id or (raw.get("effect_id") if isinstance(raw.get("effect_id"), str) else None)
    operation_ref = operation_ref or (raw.get("operation_ref") if isinstance(raw.get("operation_ref"), str) else None)
    status = _status_for(code, raw, effect=effect)
    owner = _route_for(code, raw)
    if raw.get("operator_required") is True:
        owner = RecoveryOwner.OPERATOR
    elif raw.get("recovery_owner") in _ROUTING:
        owner = _ROUTING[str(raw["recovery_owner"])]
    agent = owner is RecoveryOwner.AGENT
    operator = owner is RecoveryOwner.OPERATOR
    unknown = status is EffectStatus.UNKNOWN
    stale = status is EffectStatus.STALE
    field_path = raw.get("field_path", raw.get("path", ()))
    if isinstance(field_path, str):
        field_path = (field_path,)
    if not isinstance(field_path, (tuple, list)):
        field_path = ()
    handoff = raw.get("handoff")
    if not isinstance(handoff, Mapping):
        handoff = {
            key: raw[key]
            for key in ("run_ref", "attempt_ref", "root_session_ref", "fence_ref", "target_ref", "successor_ref")
            if key in raw
        }
    next_action = (
        NextAction.RECONCILE
        if unknown
        else NextAction.REREAD_CURRENT_STATE
        if stale
        else NextAction.CORRECT_INPUT
        if agent
        else NextAction.ESCALATE
        if operator
        else NextAction.RECONCILE
        if status is EffectStatus.REJECTED
        else NextAction.RETRY_SAFE
    )
    if (
        raw.get("next_action") in {item.value for item in NextAction}
        and status not in {
            EffectStatus.UNKNOWN,
            EffectStatus.STALE,
            EffectStatus.REJECTED,
        }
    ):
        next_action = NextAction(str(raw["next_action"]))
    correction = raw.get("correction") if isinstance(raw.get("correction"), str) else None
    if status is EffectStatus.NOT_STARTED and agent and (
        correction is None or "unknown" in correction.lower()
    ):
        correction = "Correct the indicated input or binding. This submission did not start an effect."
    audit_ref = raw.get("audit_ref") if isinstance(raw.get("audit_ref"), str) else None
    default_retry = (
        status in {EffectStatus.NOT_STARTED, EffectStatus.WAITING}
        and not agent
        and not operator
    )
    retry_allowed = bool(raw.get("retry_allowed", default_retry))
    if status in {
        EffectStatus.REJECTED,
        EffectStatus.UNKNOWN,
        EffectStatus.STALE,
        EffectStatus.CANCELLED,
    } or agent or operator:
        retry_allowed = False
    contract = ErrorContract(
        code=code,
        legacy_code=code,
        phase=str(raw.get("phase") or phase),
        operation_id=operation_id,
        operation_ref=operation_ref,
        effect_id=effect_id,
        effect_status=status,
        field_path=tuple(field_path),
        handoff=handoff,
        expected=raw.get("expected", raw.get("requirement")),
        actual=raw.get("actual"),
        feedback_stage_agent=agent and bool(raw.get("feedback_stage_agent", True)),
        correction=correction,
        retry_allowed=retry_allowed,
        reconcile_same_effect=unknown or bool(raw.get("reconcile_same_effect", False)),
        next_action=next_action,
        resume_required=bool(raw.get("resume_required", unknown and "session" in code)),
        successor_required=bool(raw.get("successor_required", False)),
        operator_required=bool(raw.get("operator_required", operator)),
        recovery_owner=owner,
        recovery_component=str(raw.get("recovery_component") or "semantic_mcp"),
        audit_ref=audit_ref,
        correlation_ref=correlation_ref or (raw.get("correlation_ref") if isinstance(raw.get("correlation_ref"), str) else None),
    )
    if audit_ref is not None:
        return contract
    facts = contract.as_details()
    del facts["audit_ref"]
    generated = _audit_identity(facts)
    return replace(contract, audit_ref=generated, _generated_audit_ref=generated)


def error_contract_from_exception(
    error: BaseException,
    *,
    phase: str,
    operation_id: str,
    effect: bool = False,
    operation_ref: str | None = None,
    effect_id: str | None = None,
    correlation_ref: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> ErrorContract:
    """Normalize any boundary exception without exposing its message."""

    code = getattr(error, "code", None)
    if not isinstance(code, str) or not code:
        code = type(error).__name__.replace("Error", "").lower() or "operation_failed"
    inherited = getattr(error, "details", None)
    merged: dict[str, Any] = {}
    if isinstance(inherited, Mapping):
        merged.update(inherited)
    if isinstance(details, Mapping):
        merged.update(details)
    if isinstance(getattr(error, "next_retry_at", None), (int, float)):
        merged.setdefault("next_retry_at", getattr(error, "next_retry_at"))
    return error_contract_from_legacy(
        code,
        merged,
        phase=phase,
        operation_id=operation_id,
        effect=effect,
        operation_ref=operation_ref,
        effect_id=effect_id,
        correlation_ref=correlation_ref,
    )


def validate_effect_transition(
    previous: EffectStatus,
    current: EffectStatus,
    *,
    terminal_evidence: bool = False,
    same_effect: bool = True,
    new_effect: bool = False,
    current_fence: bool = True,
) -> None:
    """Reject unsafe recovery transitions before an adapter attempts a write."""

    if not current_fence:
        raise ValueError("old_fence_write_blocked")
    if previous is EffectStatus.UNKNOWN and current is EffectStatus.APPLIED and not terminal_evidence:
        raise ValueError("unknown_requires_terminal_evidence")
    if previous is EffectStatus.UNKNOWN and current is EffectStatus.NOT_STARTED:
        raise ValueError("unknown_cannot_blind_retry")
    if previous is EffectStatus.UNKNOWN and current in {EffectStatus.APPLIED, EffectStatus.REJECTED, EffectStatus.WAITING} and not same_effect:
        raise ValueError("unknown_requires_same_effect_reconcile")
    if previous is EffectStatus.STALE and current in {EffectStatus.APPLIED, EffectStatus.WAITING}:
        raise ValueError("stale_effect_cannot_write")
    if previous is EffectStatus.REJECTED and current is EffectStatus.NOT_STARTED and not new_effect:
        raise ValueError("agent_correction_requires_new_effect")


__all__ = [
    "EffectStatus", "ErrorContract", "NextAction", "RecoveryOwner",
    "STABLE_ERROR_CODE_MAP",
    "error_contract_from_exception", "error_contract_from_legacy",
    "validate_effect_transition",
]
