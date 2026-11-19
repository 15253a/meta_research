from __future__ import annotations

from meta_research.error_contract import (
    EffectStatus,
    NextAction,
    RecoveryOwner,
    error_contract_from_legacy,
)


def test_unsafe_retry_metadata_cannot_override_effect_guards() -> None:
    rejected = error_contract_from_legacy(
        "capability_unavailable",
        {"reason": "not_granted", "retry_allowed": True},
        effect=True,
    )
    assert rejected.effect_status is EffectStatus.REJECTED
    assert rejected.retry_allowed is False
    assert rejected.next_action is NextAction.ESCALATE
    assert rejected.recovery_owner is RecoveryOwner.OPERATOR

    unknown = error_contract_from_legacy(
        "semantic_operation_failed",
        {
            "effect_id": "e-1",
            "retry_allowed": True,
            "reconcile_same_effect": False,
            "next_action": "retry_safe",
        },
        operation_id="agent_runtime.target_work.request",
        effect=True,
    )
    assert unknown.effect_status is EffectStatus.UNKNOWN
    assert unknown.retry_allowed is False
    assert unknown.reconcile_same_effect is True
    assert unknown.next_action is NextAction.RECONCILE

