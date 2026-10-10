import pytest

from meta_research.time_budget import time_budget_seconds, valid_time_budget


@pytest.mark.parametrize("budget,seconds", [
    ("2.5h", 9000), ("0.01h", 36), ("12.75h", 45900),
    ("7d", 604800), ("30d", 2592000), ("90d", 7776000), ("open", None),
])
def test_hours_and_legacy_budgets_convert_to_wall_clock_seconds(budget, seconds):
    assert time_budget_seconds(budget) == seconds
    assert valid_time_budget(budget)


@pytest.mark.parametrize("budget", [
    "", "h", "0h", "0.00h", "-1h", "NaNh", "Infinityh", "1e3h", "0.001h",
    "2.5d", "2h\n", "999999999999999h", 2.5, None, {}, [],
])
def test_invalid_budgets_are_rejected(budget):
    assert not valid_time_budget(budget)
    with pytest.raises(ValueError, match="time_budget_invalid"):
        time_budget_seconds(budget)
