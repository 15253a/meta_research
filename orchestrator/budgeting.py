"""单轮预算 B(t) 的**唯一定义**（policy budget 节）——compiler 与 status_card 共用，防公式两处漂移（§10）。

B(t) = min(B0 · 2^(⌊n/doubling_period_m⌋), B_max)，n = 已完成（status='done'）的 cycle 数。
float() 统一：整型 policy 也渲成 x.0（护 context_pack 字节一致——同快照同值同字节）。
"""
from __future__ import annotations

from typing import Any, Dict


def compute_budget(conn, policy_budget: Dict[str, Any]) -> float:
    n = conn.execute("SELECT count(*) FROM cycle WHERE status='done'").fetchone()[0]
    return float(min(policy_budget["B0"] * (2 ** (n // policy_budget["doubling_period_m"])), policy_budget["B_max"]))
