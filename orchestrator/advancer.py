"""SqliteAdvancer —— 编排器步进器（M3：真组件上的可恢复状态机步进 + 路由派生）。

与 M0 driver（走桩 + 真 Codex，M0 验收栈）**并存不替换**。M3 Advancer 操作**真** SQLiteStateStore/
SqliteCompiler，把一轮 cycle 按阶段推进到 done。

**恢复模型（§4.4.5，M3 核心）**：一阶段的全部状态写在**单一 `state.atomic()` 事务**内提交——
- kill-9 发生在 COMMIT 前 → 事务回滚 + 进程内投影复原 → cycle 停在阶段前状态 → 重启重做该阶段；
- kill-9 发生在 COMMIT 后 → 阶段已落 → 重启读 `cycle.status`（续跑游标）跳过。
故 `advance` 幂等：已终态 cycle 直接返回 done（重复调用不重复写）。

**恢复一致性比较（CP4.2 恢复测试作者注意）**：cycle/question/decision/… 多表带 `DEFAULT CURRENT_TIMESTAMP`
（如 `mark_cycle_done` 写 `finished_at`）。「杀 vs 不杀 → 终库一致」的等价关系**须排除 timestamp 列**（§7.1 M3
明文「排除 timestamps/attempt_id/log offset 等非确定字段」）——**不要**靠冻结时钟来消差，那会掩盖真非确定源。

**CP4.1 范围**：`derive_next_route` 全矩阵（§6.13(3)）+ `advance()` 驱动 **bootstrap** 创世轮
（reasoning-only：create_root + selection，无攻坚问题、无激活）。decompose/attack 轮（需外层驱动循环
备轮：开轮/派 route/激活目标问题）+ 池注册 = 后续检查点 / M4。见 ROADMAP 步④裁量。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Union

from .interfaces import PlanOutcome, Route, Selection, Stage

# reasoning 产物提供者：(cycle, context_pack) -> {"tree_ops.json":{...}, "selection.json":{...}, "answer.json"?:{...}}
# 生产路径 = 包 CodexRunner + schema 校验 + 重试（对齐 M0 driver._run_reasoning_with_retry，后续检查点接）；
# 测试注入确定性替身（不花 token、可复现，护恢复测试的「杀 vs 不杀→一致」可判定）。
ReasoningProvider = Callable[[Any, Any], Dict[str, Any]]


def derive_next_route(prev_selection: Selection, outcome: PlanOutcome) -> Optional[Route]:
    """§6.13(3) 路由矩阵：由 selection.next_intent + plan outcome 派生下一轮 route。

    | next_intent | plan_outcome            | route            |
    | terminate   | —                       | None（停机、不改写）|
    | decompose   | —                       | decompose        |
    | attack      | blocked / import_deferred | dependency_wait |
    | attack      | 含 build/exec           | attack           |
    | attack      | 仅 eval                 | eval_only        |
    | attack      | 空 targets 且无 blocked  | reuse_only       |

    与 M0 driver `_specialize_route` 同口径（此为真接口，M0 driver 保留其内联版供 M0 验收栈）。
    """
    intent = prev_selection.next_intent
    if intent == "terminate":
        return None                      # 停机：本轮 route 不改写（持久停机标志在 selection.next_question_id=None）
    if intent == "decompose":
        return "decompose"
    if intent != "attack":
        raise ValueError(f"next_intent ∉ {{attack,decompose,terminate}}: {intent!r}")
    # intent == "attack"：按 plan outcome 特化。**fail closed**：reuse_only 仅限「空 targets」——
    # 不把「无任何 flag」静默当全复用（那会掩盖分类器 bug / 未来新 target kind 未接，codex SHOULD）。
    if outcome.blocked or outcome.import_deferred:
        return "dependency_wait"         # 本轮新写未满足 dep（撞占用 / import deferred）
    if outcome.has_build_or_exec:
        return "attack"
    if outcome.only_eval:
        return "eval_only"
    if outcome.empty_targets:
        return "reuse_only"              # 空 targets 且无 blocked（全命中复用）
    raise ValueError(
        "attack 的 plan outcome 无法分类（非 blocked/import/build-exec/only-eval/empty）——"
        "疑分类器 bug 或未来新 target kind 未接；fail closed，不静默当全复用")


class SqliteAdvancer:
    def __init__(self, state, compiler, reasoning_provider: ReasoningProvider,
                 gate=None, recall=None):
        """state = SQLiteStateStore；compiler = SqliteCompiler；reasoning_provider 见模块注释。
        gate/recall 为后续检查点（attack 轮 close_question / 检索）预留，CP4.1 未用。"""
        self.state = state
        self.compiler = compiler
        self._reasoning = reasoning_provider
        self.gate = gate
        self.recall = recall

    def derive_next_route(self, prev_selection: Selection, outcome: PlanOutcome) -> Optional[Route]:
        return derive_next_route(prev_selection, outcome)

    def advance(self, cycle_id: str) -> Union[Stage, str]:
        """把 cycle 推进一格并落库，返回下一 stage 或 "done"。**幂等**：已终态直接 done。

        CP4.1：仅驱动 bootstrap 创世轮（reasoning-only，一格即到 done）。"""
        cyc = self.state.cycle(cycle_id)
        if cyc.status in ("done", "failed", "aborted"):
            return "done"                # 幂等 / 恢复：已提交轮跳过（不重复写；权威判定在写事务内二次核，见 _bootstrap_cycle）
        if cyc.route != "bootstrap":
            raise NotImplementedError(
                f"CP4.1 仅驱动 bootstrap 创世轮；decompose/attack route 待后续检查点（route={cyc.route!r}）")
        self._bootstrap_cycle(cyc)
        return "done"

    def _bootstrap_cycle(self, cyc) -> None:
        """bootstrap 创世轮：render→取产物→**单一 atomic 事务**落 tree_ops(create_root) + selection + mark_done。

        - 长操作（render / provider = Codex）在事务**外**（§6.13 铁律：绝不持写事务）；只把短写序列裹进 atomic。
        - atomic 契约（statestore）：块内任一写抛异常须传播中止整事务（半写随事务回滚、投影复原）——本方法不吞异常，
          故 kill-9 / 校验失败都留下干净的「阶段前」状态供重做（恢复语义）。
        - **fail closed**：bootstrap 必产含 create_root 的 tree_ops（创世无根 = 系统无题可攻）+ 必产 selection。"""
        pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="reasoning")
        files = self._reasoning(cyc, pack)   # 长操作（render / provider=Codex）在事务外（§6.13 铁律）
        with self.state.atomic():
            # 二次核终态**优先于产物校验**（TOCTOU 安全，对齐 gate_close_question 写锁内重跑）：并发/重入若已把
            # 本轮推进到终态则跳过、不重复 create_root，也**不因本次冗余/畸形产物误报 raise**（codex 第2轮 SHOULD）。
            # WriteDaemon 单写连接 + BEGIN IMMEDIATE 保证此 re-read 见已提交的推进。
            if self.state.cycle(cyc.cycle_id).status in ("done", "failed", "aborted"):
                return
            # fail-closed 产物校验（纯字典检查、非长操作，故置于事务内、终态核之后）：bootstrap 必含 create_root + selection
            if "tree_ops.json" not in files:
                raise ValueError("bootstrap 轮必产 tree_ops.json（create_root 创世）")
            ops = files["tree_ops.json"].get("ops", [])
            if not any(op.get("op") == "create_root" for op in ops):
                raise ValueError("bootstrap 轮 tree_ops 须含 create_root（创世无根=系统无题可攻，fail closed）")
            if "selection.json" not in files:
                raise ValueError("reasoning 产物缺 selection.json（reasoning 必产；生产路径由 provider 上游 schema 校验保证）")
            sel = files["selection.json"]
            self.state.apply_tree_ops(cyc.cycle_id, ops)
            self.state.persist_selection(cyc.cycle_id, Selection(
                next_question_id=sel.get("next_question_id"),
                next_intent=sel["next_intent"],
                scores=sel.get("scores", [])))
            self.state.mark_cycle_done(cyc.cycle_id)
