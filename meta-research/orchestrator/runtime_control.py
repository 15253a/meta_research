"""Durable runtime control projections.

The frozen schema intentionally has no mutable ``runtime_policy`` table.  A
successfully consumed ``set_budget`` directive is therefore represented by its
append-only human decision; the latest such decision contains the *complete*
effective budget.  Every budget consumer derives the same projection from that
decision, so a process restart cannot silently restore the boot-time YAML
values.

Only budget limits/scheduling may be changed at runtime.  Token pricing remains
part of the versioned policy: retroactively changing the exchange rate would
make old and new ledger rows incomparable.  Arming/disarming cost accounting
(``session_max`` finite versus ``null``) also requires a policy restart; a live
directive may adjust an already armed finite ceiling but may not turn the
fail-closed accounting boundary on or off.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Mapping


MUTABLE_BUDGET_FIELDS = frozenset({"B0", "doubling_period_m", "B_max", "session_max"})
_SCHEDULE_FIELDS = ("B0", "doubling_period_m", "B_max")


def _finite_positive(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"budget.{name} 须为有限正数")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"budget.{name} 须为有限正数") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"budget.{name} 须为有限正数")
    return number


def validate_budget_config(config: Mapping[str, Any], *, require_schedule: bool = True,
                           require_session: bool = True) -> Dict[str, Any]:
    """Validate and normalize a budget projection without mutating its input."""
    if not isinstance(config, Mapping):
        raise ValueError("budget 配置须为 object")
    out = dict(config)
    if require_session and "session_max" not in out:
        raise ValueError("budget.session_max 必须显式存在")
    if "session_max" in out and out["session_max"] is not None:
        out["session_max"] = _finite_positive(out["session_max"], name="session_max")

    if require_schedule:
        missing = [name for name in _SCHEDULE_FIELDS if name not in out]
        if missing:
            raise ValueError(f"budget 缺运行期调度字段: {missing}")
        out["B0"] = _finite_positive(out["B0"], name="B0")
        out["B_max"] = _finite_positive(out["B_max"], name="B_max")
        period = out["doubling_period_m"]
        if (isinstance(period, bool) or not isinstance(period, (int, float))
                or not math.isfinite(float(period)) or not float(period).is_integer()
                or period <= 0):
            raise ValueError("budget.doubling_period_m 须为正整数")
        out["doubling_period_m"] = int(period)
        if out["B0"] > out["B_max"]:
            raise ValueError("budget.B0 不得大于 budget.B_max")

    if "price_per_1k_tokens" in out:
        price = out["price_per_1k_tokens"]
        if isinstance(price, bool):
            raise ValueError("budget.price_per_1k_tokens 须为有限非负数")
        try:
            price_f = float(price)
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError("budget.price_per_1k_tokens 须为有限非负数") from error
        if not math.isfinite(price_f) or price_f < 0:
            raise ValueError("budget.price_per_1k_tokens 须为有限非负数")
        if out.get("session_max") is not None and price_f <= 0:
            raise ValueError("budget.session_max 已启用时 price_per_1k_tokens 必须为有限正数")
        out["price_per_1k_tokens"] = price_f
    return out


def apply_budget_patch(current: Mapping[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the canonical complete budget after a safe live override."""
    current_n = validate_budget_config(current)
    if not isinstance(patch, Mapping) or not patch:
        raise ValueError("set_budget 须给出至少一个预算字段")
    unknown = sorted(set(patch) - MUTABLE_BUDGET_FIELDS)
    if unknown:
        raise ValueError(f"set_budget 不允许修改字段: {unknown}")
    updated = {**current_n, **dict(patch)}
    updated_n = validate_budget_config(updated)
    if (current_n["session_max"] is None) != (updated_n["session_max"] is None):
        raise ValueError("运行期不得启用/关闭成本记账；请修改 policy 后重启（只允许调整既有有限上限）")
    # Pricing is deliberately inherited from the versioned base policy and is
    # never accepted in a live patch (unknown-field validation above enforces it).
    return updated_n


def effective_budget_config(conn, base_budget: Mapping[str, Any], *,
                            require_schedule: bool = True,
                            require_session: bool = True) -> Dict[str, Any]:
    """Project the latest *successfully consumed* set_budget decision.

    The join to ``directive`` and equality with ``consumed_decision_id`` keep an
    arbitrary similarly named decision from becoming runtime authority.
    Corrupt JSON/effect shape fails loudly instead of falling back to YAML.
    """
    row = conn.execute(
        "SELECT d.payload_json FROM decision d JOIN directive x ON x.id=d.directive_id "
        "WHERE d.actor='human' AND d.type='directive_set_budget' "
        "AND x.kind='set_budget' AND x.status='consumed' AND x.consumed_decision_id=d.id "
        "ORDER BY d.id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return validate_budget_config(base_budget, require_schedule=require_schedule,
                                      require_session=require_session)
    try:
        payload = json.loads(row[0])
        budget = payload["effect"]["budget"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("directive_set_budget 决策缺完整 effect.budget，拒绝回退启动策略") from error
    projected = validate_budget_config(budget, require_schedule=require_schedule,
                                       require_session=require_session)
    # Runtime controls may not rewrite token pricing behind existing ledger rows.
    if "price_per_1k_tokens" in base_budget:
        base_price = validate_budget_config(
            base_budget, require_schedule=require_schedule,
            require_session=require_session).get("price_per_1k_tokens")
        if projected.get("price_per_1k_tokens") != base_price:
            raise RuntimeError("directive_set_budget 决策篡改 price_per_1k_tokens")
    return projected


def policy_with_effective_budget(policy: Mapping[str, Any], budget: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the content-fingerprint input for a runtime-controlled ledger row."""
    return {**dict(policy), "budget": dict(budget)}
