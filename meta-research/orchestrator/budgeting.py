"""单轮预算 B(t) 的**唯一定义**——compiler 与 status_card 共用，防公式两处漂移（§10）。

B(t) = min(B0 · 2^(⌊n/doubling_period_m⌋), B_max)，n = 已完成（status='done'）的 cycle 数。
参数来自 policy budget；若已有成功消费的 set_budget，则投影其完整耐久预算。
float() 统一：整型 policy 也渲成 x.0（护 context_pack 字节一致——同快照同值同字节）。
"""
from __future__ import annotations

from typing import Any, Dict

from .runtime_control import effective_budget_config


def compute_budget(conn, policy_budget: Dict[str, Any]) -> float:
    # Keep the original public contract: callers interested only in B(t) may
    # supply the three schedule fields without cost-accounting/session fields.
    budget = effective_budget_config(conn, policy_budget, require_session=False)
    n = conn.execute("SELECT count(*) FROM cycle WHERE status='done'").fetchone()[0]
    return float(min(budget["B0"] * (2 ** (n // budget["doubling_period_m"])), budget["B_max"]))
