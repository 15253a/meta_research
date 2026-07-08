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

**M3 范围（CP4.1-4.2）**：`derive_next_route` 全矩阵（§6.13(3)）+ `advance()` 驱动 **reasoning-only 轮**
（bootstrap create_root / decompose add_children，单一 atomic 阶段）+ `run_cycles()` 外层驱动循环
（durable 恢复：开轮/据上轮 selection 派 route/激活目标/续跑在途轮）。**attack 轮**（idea/plan/bundle 阶段
+ 池注册 gate_register_* + 真执行）= **M4**。见 ROADMAP 步④裁量。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Union

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

    # -- 外层驱动循环（reasoning-only：bootstrap→decompose→terminate）-----------
    def run_cycles(self, max_cycles: int) -> List[str]:
        """驱动 reasoning-only 循环到停机或 max_cycles。**恢复**：全程从 DB 读、无进程内记忆——重启新建
        Advancer 指向同库即续跑（durable 交接：下一轮 route/目标由上一 done 轮的 selection 定，非内存持有）。
        每轮：取在途轮（续跑）或据上轮 selection 开新轮 + setup（route + decompose 激活目标）→ advance 到 done。
        上轮 selection.next_intent=terminate → 停机。返回本次推进的 cycle_id 序列。"""
        ids: List[str] = []
        for _ in range(max_cycles):
            cyc = self._resume_or_open()
            if cyc is None:
                break                        # 停机（上轮 terminate）
            self.advance(cyc.cycle_id)
            ids.append(cyc.cycle_id)
        return ids

    def _resume_or_open(self):
        """取本轮要推进的（已 setup 的）cycle；None = 应停机。恢复安全：先看在途轮（续跑其未竟阶段），
        无在途轮再据上轮 selection 决定开新轮还是停机。"""
        cyc = self.state.inflight_cycle()
        if cyc is None:
            prior = self.state.last_done_cycle()
            if prior is not None:
                if prior.next_intent == "terminate":
                    return None              # 上轮选择停机（持久标志）
                # **开新轮前**就按 intent 拒不支持的续轮（attack=M4）——否则会先落一个 route=None 死轮污染 DB（codex SHOULD）。
                if prior.next_intent != "decompose":
                    raise NotImplementedError(
                        f"CP4.2 驱动循环续轮仅 decompose；next_intent={prior.next_intent!r}（attack 需池注册/真执行 = M4）")
            cyc = self.state.open_or_resume_cycle()   # 无在途轮 → 创建新轮（created, route=None）
        if cyc.route is None:                # 新轮/未 setup → 定 route + decompose 激活目标
            self._setup_cycle(cyc)
            cyc = self.state.cycle(cyc.cycle_id)
        return cyc

    def _setup_cycle(self, cyc) -> None:
        """据上一 done 轮 selection 定本轮 route（首轮 bootstrap）+ decompose 激活目标——**单一 atomic 事务**
        （恢复：kill 前回滚→route 仍 None 重 setup；后→已 setup，二次核 route 跳过，防并发/重入重复 setup）。
        续轮的 unsupported-intent 拒绝在 _resume_or_open 前置（此处 prior.next_intent 已保证 decompose）；
        本方法的 intent 判仍保留为防御（直接调用本方法的入口，非经 run_cycles）。"""
        with self.state.atomic():
            if self.state.cycle(cyc.cycle_id).route is not None:
                return                       # 已 setup（并发/重入）
            prior = self.state.last_done_cycle()   # 事务内读（与 route 二次核同一快照，NIT）；done 轮不可变，读位不影响正确性
            if prior is None:
                self.state.set_route(cyc.cycle_id, "bootstrap")        # 首轮创世
                return
            if prior.next_intent != "decompose":                      # 防御（run_cycles 已前置拒）
                raise NotImplementedError(
                    f"驱动循环续轮仅 decompose；next_intent={prior.next_intent!r}（attack 需池注册/真执行 = M4）")
            route = derive_next_route(   # decompose → decompose（走矩阵，canonical 路由源）
                Selection(next_question_id=prior.next_question_id, next_intent="decompose"), PlanOutcome())
            self.state.set_route(cyc.cycle_id, route)
            self.state.activate_question(prior.next_question_id)       # 目标问题 open→active（同一 setup 事务，护恢复）

    # -- 单轮推进 --------------------------------------------------------------
    def advance(self, cycle_id: str) -> Union[Stage, str]:
        """把 cycle 推进一格并落库，返回下一 stage 或 "done"。**幂等**：已终态直接 done。

        CP4.2：驱动 reasoning-only 轮（bootstrap/decompose），一格即到 done。attack route = M4（需池注册/真执行）。"""
        cyc = self.state.cycle(cycle_id)
        if cyc.status in ("done", "failed", "aborted"):
            return "done"                # 幂等 / 恢复：已提交轮跳过（不重复写；权威判定在写事务内二次核，见 _reasoning_cycle）
        if cyc.route not in ("bootstrap", "decompose"):
            raise NotImplementedError(
                f"CP4.2 仅驱动 reasoning-only 轮（bootstrap/decompose）；attack route = M4（需池注册/真执行）（route={cyc.route!r}）")
        self._reasoning_cycle(cyc)
        return "done"

    def _reasoning_cycle(self, cyc) -> None:
        """reasoning-only 轮（bootstrap/decompose）：render→取产物→**单一 atomic 事务**落 tree_ops + selection + mark_done。

        - 长操作（render / provider = Codex）在事务**外**（§6.13 铁律：绝不持写事务）；只把短写序列裹进 atomic。
        - atomic 契约（statestore）：块内任一写抛异常须传播中止整事务（半写随事务回滚、投影复原）——本方法不吞异常，
          故 kill-9 / 校验失败都留下干净的「阶段前」状态供重做（恢复语义）。
        - **fail closed**：bootstrap 须含 create_root（创世无根=系统无题可攻）；decompose 须含 add_children（对活跃父）。"""
        pack = self.compiler.render(cycle_id=cyc.cycle_id, stage="reasoning")
        files = self._reasoning(cyc, pack)   # 长操作（render / provider=Codex）在事务外（§6.13 铁律）
        with self.state.atomic():
            # 二次核终态**优先于产物校验**（TOCTOU 安全，对齐 gate_close_question 写锁内重跑）：并发/重入若已把本轮推进到
            # 终态则跳过、不重复写，也不因本次冗余/畸形产物误报 raise。WriteDaemon 单写连接 + BEGIN IMMEDIATE 保证 re-read 见已提交推进。
            fresh = self.state.cycle(cyc.cycle_id)
            if fresh.status in ("done", "failed", "aborted"):
                return
            if fresh.route not in ("bootstrap", "decompose"):   # 防御：route 本不可变，二次核补全 TOCTOU 契约（NIT）
                raise RuntimeError(f"reasoning-only 轮 route 在途被改？{fresh.route!r}")
            ops = self._validated_ops(files, fresh.route)
            if "selection.json" not in files:
                raise ValueError("reasoning 产物缺 selection.json（reasoning 必产；生产路径由 provider 上游 schema 校验保证）")
            sel = files["selection.json"]
            self.state.apply_tree_ops(cyc.cycle_id, ops)
            self.state.persist_selection(cyc.cycle_id, Selection(
                next_question_id=sel.get("next_question_id"),
                next_intent=sel["next_intent"],
                scores=sel.get("scores", [])))
            self.state.mark_cycle_done(cyc.cycle_id)

    @staticmethod
    def _validated_ops(files: Dict[str, Any], route: str) -> list:
        """fail-closed 产物校验：reasoning-only 轮必产 tree_ops，且 bootstrap 须含 create_root、decompose 须含 add_children。"""
        if "tree_ops.json" not in files:
            raise ValueError(f"{route} 轮必产 tree_ops.json")
        ops = files["tree_ops.json"].get("ops", [])
        need = "create_root" if route == "bootstrap" else "add_children"
        if not any(op.get("op") == need for op in ops):
            raise ValueError(f"{route} 轮 tree_ops 须含 {need}（fail closed；创世/分解无实质 op = 空转/无题）")
        return ops
