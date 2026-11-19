from __future__ import annotations

import pytest

from meta_research.error_contract import (
    EffectStatus,
    NextAction,
    RecoveryOwner,
    error_contract_from_legacy,
    error_contract_from_exception,
    validate_effect_transition,
)


def test_agent_input_error_preserves_legacy_code_and_field_path() -> None:
    error = error_contract_from_legacy(
        "semantic_input_schema_mismatch",
        {"path": ["target_ref"], "requirement": "present", "call_execution": "not_started"},
        phase="input_validation",
        operation_id="agent_runtime.target_work.request",
    )

    assert error.effect_status is EffectStatus.NOT_STARTED
    assert error.recovery_owner is RecoveryOwner.AGENT
    assert error.feedback_stage_agent is True
    assert error.field_path == ("target_ref",)
    assert error.next_action is NextAction.CORRECT_INPUT
    assert error.stable_code == "input.invalid"
    assert error.as_public_dict()["details"]["legacy_code"] == "semantic_input_schema_mismatch"


def test_unknown_effect_requires_same_effect_reconcile() -> None:
    error = error_contract_from_legacy(
        "semantic_operation_failed",
        {"effect_id": "dispatch-1", "operation_ref": "op-1"},
        phase="handler",
        operation_id="agent_runtime.target_work.request",
        effect=True,
    )

    assert error.effect_status is EffectStatus.UNKNOWN
    assert error.reconcile_same_effect is True
    assert error.retry_allowed is False
    assert error.next_action is NextAction.RECONCILE


def test_stale_fence_routes_to_system_and_never_retries() -> None:
    error = error_contract_from_legacy(
        "attempt_fence_stale",
        {"fence_ref": "old-fence", "expected": "current"},
        operation_id="agent_runtime.target_work.request",
        effect=True,
    )

    assert error.effect_status is EffectStatus.STALE
    assert error.recovery_owner is RecoveryOwner.SYSTEM
    assert error.retry_allowed is False
    assert error.next_action is NextAction.REREAD_CURRENT_STATE


def test_error_projection_sanitizes_exception_objects() -> None:
    error = error_contract_from_legacy(
        "semantic_operation_failed",
        {"actual": RuntimeError("secret path")},
        operation_id="agent_runtime.target_work.request",
        effect=True,
    )

    assert error.as_details()["actual"] == "<RuntimeError>"
    assert "secret path" not in str(error.as_public_dict())


def test_unsafe_effect_transitions_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown_cannot_blind_retry"):
        validate_effect_transition(EffectStatus.UNKNOWN, EffectStatus.NOT_STARTED)
    with pytest.raises(ValueError, match="unknown_requires_terminal_evidence"):
        validate_effect_transition(EffectStatus.UNKNOWN, EffectStatus.APPLIED)
    with pytest.raises(ValueError, match="unknown_requires_same_effect_reconcile"):
        validate_effect_transition(
            EffectStatus.UNKNOWN,
            EffectStatus.WAITING,
            same_effect=False,
        )
    with pytest.raises(ValueError, match="agent_correction_requires_new_effect"):
        validate_effect_transition(EffectStatus.REJECTED, EffectStatus.NOT_STARTED)
    with pytest.raises(ValueError, match="old_fence_write_blocked"):
        validate_effect_transition(
            EffectStatus.NOT_STARTED,
            EffectStatus.APPLIED,
            terminal_evidence=True,
            current_fence=False,
        )


def test_capability_not_granted_is_operator_rejected() -> None:
    error = error_contract_from_legacy(
        "capability_unavailable",
        {"reason": "not_granted"},
        operation_id="agent_runtime.target_work.request",
        effect=True,
    )

    assert error.effect_status is EffectStatus.REJECTED
    assert error.recovery_owner is RecoveryOwner.OPERATOR
    assert error.operator_required is True
    assert error.retry_allowed is False


def test_exception_mapper_preserves_retry_metadata_without_message() -> None:
    class Failure(RuntimeError):
        code = "provider_timeout"
        next_retry_at = 12.5

    error = error_contract_from_exception(
        Failure("secret provider path"),
        phase="harness",
        operation_id="target.harness",
        effect=True,
    )

    assert error.effect_status is EffectStatus.UNKNOWN
    assert error.reconcile_same_effect is True
    assert error.as_details()["actual"] is None
    assert "secret provider path" not in str(error.as_public_dict())

