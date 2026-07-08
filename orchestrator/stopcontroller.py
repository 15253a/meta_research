"""StopController —— 长跑自终止安全网（§4.4.6 τ 三判据；M6 CP7.1）。

**为什么要它**：run_cycles 原本只在 reasoning provider 返回 next_intent='terminate' 时停机（=τ判据③
目标谓词满足，由 Codex 判定）。「数百轮无人值守不漂移」要求编排器**自己**能停——防 provider 永不
terminate 时无限空转。本控制器补齐另两条安全网：

- **判据① ·价值衰退**：open/active 问题最高分连续 N 轮 < score_floor（policy.flow.tau；作用域含
  open+active，对齐 §4.4.6/ROADMAP「open/active 最高分」）。**只在整条前沿都已评分时**判定衰退——
  只要有**任一** open/active 题分数为 NULL（未评估=价值未知≠低于 τ），本轮不算衰退（严格保守，防
  「一个低分 + 一个未评」的混合前沿误停活跃研究）。计数在进程内（run_cycles 每轮完成后 tick 一次）。
  **恢复语义**：进程重启计数归零 = **保守**（只会延后停机，绝不误停/漂移）；一旦真触发即落 durable
  global_stop 决策，重启不再推进（下方 already_stopped）。
- **判据② ·预算耗尽**：全局成本台账 `ledger.money` 求和（§ ledger 覆盖所有 phase——含 reasoning/Codex
  调用，是「全局成本」的**唯一权威源**）≥ policy.budget.session_max。DB 派生、恢复安全。session_max=null
  关闭本条。**当前状态**：ledger 写入（成本记账）尚未接线（M6 硬化项）——本网**已装、待成本落账即生效**
  （在此之前 SUM=0、不误触发）。故不用 run.cost/attempt.cost（那是分执行的局部成本、且漏掉 reasoning）。
- **判据③ ·谓词满足**：仍由 provider terminate 走（不在此——它是研究判断，Codex 出；本控制器只管
  「provider 该停却不停」的安全网）。

**停机 = durable**：record_stop 写 DECISION(actor='orchestrator', type='global_stop', payload={reason})；
already_stopped 检测其存在 → run_cycles 启动即拒推进（判据①的进程内计数丢失也不影响：停机事实已落库）。

**恢复安全的预算门**（外审 BLOCKER）：预算判据是 DB 派生、纯读——故 run_cycles **每轮开工前**（含首轮、
重启后首轮）先跑 check_before_round：若 ledger 已超限但上次崩在落 global_stop 之前，重启不会先白跑一
整轮才停。分数 streak 有进程内副作用、只在轮**后** tick（check_after_round），语义不变。

**单活 orchestrator 假设**：全系统单写（WriteDaemon 单连接）、单编排器驱动。record_stop 的存在检查与
插入在**同一事务**内（原子幂等）；此假设下不会有并发写出多条 global_stop。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .writedaemon import WriteDaemon


class StopController:
    def __init__(self, daemon: WriteDaemon, policy: Dict[str, Any]):
        self.daemon = daemon
        tau = policy.get("flow", {}).get("tau", {})
        self.score_floor = tau.get("score_floor")
        self.consecutive_rounds = tau.get("consecutive_rounds")
        self.session_max = policy.get("budget", {}).get("session_max")   # None = 关闭预算安全网
        self._below_floor_streak = 0                                     # 判据① 进程内连续计数

    # ---------------------------------------------------------------- durable --
    def already_stopped(self) -> Optional[str]:
        """已落 global_stop 决策 → 返回原因（run_cycles 启动即拒推进）；否则 None。"""
        r = self.daemon.query_one(
            "SELECT payload_json FROM decision WHERE actor='orchestrator' AND type='global_stop' "
            "ORDER BY id DESC LIMIT 1")
        return json.loads(r[0]).get("reason") if r else None

    def record_stop(self, reason: str, detail: Optional[Dict[str, Any]] = None) -> None:
        """落 durable 停机决策（原子幂等：存在检查与插入同事务，单活 orchestrator 下不重复写）。"""
        with self.daemon.transaction() as conn:
            if conn.execute("SELECT 1 FROM decision WHERE actor='orchestrator' AND type='global_stop' "
                            "LIMIT 1").fetchone() is not None:
                return
            # {**detail, reason} 序：入参 reason 权威，不被 detail 里同名键覆盖（防未来调用点传 detail.reason）
            conn.execute("INSERT INTO decision(actor,type,payload_json) VALUES ('orchestrator','global_stop',?)",
                         (json.dumps({**(detail or {}), "reason": reason}, ensure_ascii=False),))

    # ---------------------------------------------------------------- 判据 --
    def _budget_exhausted(self) -> Optional[Dict[str, Any]]:
        if self.session_max is None:
            return None
        spent = self.daemon.query_one("SELECT COALESCE(SUM(money),0) FROM ledger")[0]
        if spent >= self.session_max:
            return {"reason": "budget_exhausted", "spent": spent, "session_max": self.session_max}
        return None

    def _score_floor_hit(self) -> Optional[Dict[str, Any]]:
        """判据①：更新连续计数并在达阈时返回停因。**每轮完成后调用恰一次**（计数语义依赖此节拍）。
        衰退 = open/active 前沿**全部已评分** 且 最高分 < floor；有任一未评分（NULL）或无 open/active
        题 → 非衰退（重置）——未评估的价值未知，不能当作低于 τ（严格保守，防混合前沿误停）。"""
        if self.score_floor is None or self.consecutive_rounds is None:
            return None
        total, scored, top = self.daemon.query_one(
            "SELECT COUNT(*), COUNT(score), MAX(score) FROM question WHERE status IN ('open','active')")
        fully_scored_frontier = total > 0 and scored == total
        if fully_scored_frontier and top < self.score_floor:
            self._below_floor_streak += 1
        else:
            self._below_floor_streak = 0            # 无题 / 有未评题 / 有高分题 → 打断
        if self._below_floor_streak >= self.consecutive_rounds:
            return {"reason": "score_floor", "top_frontier_score": top, "score_floor": self.score_floor,
                    "streak": self._below_floor_streak}
        return None

    def check_before_round(self) -> Optional[Dict[str, Any]]:
        """**每轮开工前**评估恢复安全的预算门（无 score streak 副作用；含重启后首轮）：命中会 record_stop
        落 durable global_stop 并返回。分数 streak 不在此推进（它是轮后语义）——故预算超限在重启后立即
        停、不白跑一轮。"""
        hit = self._budget_exhausted()
        if hit is not None:
            self.record_stop(hit["reason"], hit)
        return hit

    def check_after_round(self) -> Optional[Dict[str, Any]]:
        """一轮完成后评估安全网（run_cycles 每轮调用一次）：命中即 record_stop 并返回停因；否则 None。
        顺序：预算（durable、无副作用）先于分数（有计数副作用）——预算命中时不推进分数计数无碍。"""
        hit = self._budget_exhausted() or self._score_floor_hit()
        if hit is not None:
            self.record_stop(hit["reason"], hit)
        return hit
