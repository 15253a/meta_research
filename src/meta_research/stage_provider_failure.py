"""The common quiescence boundary after role-specific provider recovery."""
from __future__ import annotations

from typing import Protocol


class _ProviderFailure(Protocol):
    code: str
    recovery_checkpoint: dict[str, object] | None


class _RecordHardCeiling(Protocol):
    def __call__(
        self, *, failure_code: str, provider_exit: dict[str, object]
    ) -> object: ...


def settle_stage_provider_failure(
    error: _ProviderFailure, record_hard_ceiling: _RecordHardCeiling
) -> bool:
    """Return whether the exact real operation can acknowledge a safe point.

    The caller handles its HR and candidate contracts first and clears its
    safe-point flag before calling: a failed Owner write must never authorize
    an ACK. Unknown transport remains bound to its original operation.
    """
    if error.code == "codex_operation_reconciliation_pending":
        return False
    if error.recovery_checkpoint is not None:
        record_hard_ceiling(
            failure_code=error.code, provider_exit=error.recovery_checkpoint
        )
        return False
    return True
