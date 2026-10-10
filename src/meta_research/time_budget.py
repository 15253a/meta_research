"""Quest wall-clock budgets, including editable decimal hours."""

from decimal import Decimal
import re


_LEGACY_SECONDS = {
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
    "90d": 90 * 24 * 3600,
    "open": None,
}
_HOURS = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?h\Z")
_MAX_SAFE_SECONDS = 9_007_199_254_740_991


def time_budget_seconds(value: object) -> int | None:
    if not isinstance(value, str):
        raise ValueError("time_budget_invalid")
    if value in _LEGACY_SECONDS:
        return _LEGACY_SECONDS[value]
    if len(value) > 24 or not _HOURS.fullmatch(value):
        raise ValueError("time_budget_invalid")
    seconds = int(Decimal(value[:-1]) * 3600)
    if seconds <= 0 or seconds > _MAX_SAFE_SECONDS:
        raise ValueError("time_budget_invalid")
    return seconds


def valid_time_budget(value: object) -> bool:
    try:
        time_budget_seconds(value)
    except ValueError:
        return False
    return True
