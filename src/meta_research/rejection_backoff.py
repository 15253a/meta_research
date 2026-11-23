from __future__ import annotations

import os


def rejection_backoff_config() -> tuple[int, float, float, int]:
    """Return (backoff_after, base_seconds, max_seconds, max_consecutive)."""
    after = max(1, int(os.environ.get("META_RESEARCH_REJECTION_BACKOFF_AFTER", "2")))
    base = max(0.0, float(os.environ.get("META_RESEARCH_REJECTION_BACKOFF_BASE_SECONDS", "10.0")))
    maximum = max(base, float(os.environ.get("META_RESEARCH_REJECTION_MAX_SECONDS", os.environ.get("META_RESEARCH_REJECTION_BACKOFF_MAX_SECONDS", "300.0"))))
    max_consecutive = max(after, int(os.environ.get("META_RESEARCH_REJECTION_MAX_CONSECUTIVE", "8")))
    return after, base, maximum, max_consecutive


def evaluate_rejection_backoff(
    *,
    consecutive_rejections: int,
    anchor_at: float,
    now: float,
) -> tuple[str | None, float | None]:
    """Classify sequential rejections into (limit_or_wait_kind, next_retry_at).

    Returns:
    - ("limit_reached", None) when consecutive_rejections >= max_consecutive
    - ("backoff_pending", next_retry_at) when consecutive_rejections >= backoff_after and now < next_retry_at
    - (None, None) when execution may proceed immediately
    """
    if consecutive_rejections < 1:
        return None, None
    backoff_after, base_seconds, max_seconds, max_consecutive = rejection_backoff_config()
    if consecutive_rejections >= max_consecutive:
        return "limit_reached", None
    if consecutive_rejections >= backoff_after and base_seconds > 0.0:
        delay = min(
            base_seconds * (2 ** min(consecutive_rejections - backoff_after, 30)),
            max_seconds,
        )
        next_retry_at = anchor_at + delay
        if now < next_retry_at:
            return "backoff_pending", next_retry_at
    return None, None
