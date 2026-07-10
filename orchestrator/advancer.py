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

from .cost_ledger import BudgetExhausted, CostAccountingFailed
from .interfaces import PlanOutcome, Route, Selection, Stage, StageBlockedOnResources

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
                 gate=None, recall=None, attack=None, status_publisher=None, precheck=None,
                 stop_controller=None):
        """state = SQLiteStateStore；compiler = SqliteCompiler；reasoning_provider 见模块注释。
        attack = attack_stages.AttackStages（M4 CP5.4：idea/plan/bundle/reasoning 阶段推进；None = 拒 attack 轮）。
        status_publisher = status_card.SqliteStatusPublisher（M5 CP6.2：阶段边界原子发布人机快照；
        None = 不发布——发布是派生观测，不参与研究状态机，故可选装）。
        precheck = notify.make_advancer_precheck 产物（M5 CP6.3，§4.4.1 全局等待）：callable(cyc_or_None)
        -> Optional[str]——先按时机消费到期 directive，再返回阻断拒因（pause 生效中 / pending 文件请求）；
        非 None ⇒ 本轮 run_cycles 停止推进（不发新研究 Runner 调用、不推阶段；query/通知不走本类、照常）。
        gate/recall 预留。"""
        self.state = state
        self.compiler = compiler
        self._reasoning = reasoning_provider
        self.gate = gate
        self.recall = recall
        self.attack = attack
        self.status_publisher = status_publisher
        self.precheck = precheck
        self.stop_controller = stop_controller   # M6 CP7.1：长跑自终止安全网（§4.4.6 τ）；None = 不装（只认 provider terminate）
        self.last_block_reason: Optional[str] = None   # 最近一次阻断拒因（观测用；None=未被阻断）
        self.last_stop_reason: Optional[str] = None    # 最近一次自终止停因（None=未自停）
        self.import_worker = None    # M4 CP5.5：物化 worker（set 后 _resume_or_open 识别 worker 轮交其续跑）

    def derive_next_route(self, prev_selection: Selection, outcome: PlanOutcome) -> Optional[Route]:
        return derive_next_route(prev_selection, outcome)

    # -- 外层驱动循环（reasoning-only：bootstrap→decompose→terminate）-----------
    def run_cycles(self, max_cycles: int) -> List[str]:
        """驱动 reasoning-only 循环到停机或 max_cycles。**恢复**：全程从 DB 读、无进程内记忆——重启新建
        Advancer 指向同库即续跑（durable 交接：下一轮 route/目标由上一 done 轮的 selection 定，非内存持有）。
        每轮：取在途轮（续跑）或据上轮 selection 开新轮 + setup（route + decompose 激活目标）→ advance 到 done。
        上轮 selection.next_intent=terminate → 停机。返回本次推进的 cycle_id 序列。"""
        ids: List[str] = []
        if self.stop_controller is not None:     # durable 停机（§4.4.6）：已落 global_stop 决策 → 拒推进
            self.last_stop_reason = self.stop_controller.already_stopped()
            if self.last_stop_reason is not None:
                return ids
        for _ in range(max_cycles):
            if self.stop_controller is not None:  # 恢复安全预算门（§4.4.6 判据②）：每轮开工前（含重启后首轮）
                hit = self.stop_controller.check_before_round()   # ledger 已超限则立即停、不白跑一轮
                if hit is not None:
                    self.last_stop_reason = hit["reason"]
                    break
            if self._blocked(None):
                break                        # 全局等待（§4.4.1）：开新轮前被阻断（pause / pending 文件请求）
            cyc = self._resume_or_open()
            if cyc is None:
                break                        # 停机（上轮 terminate）
            for _step in range(8):               # attack 轮多阶段：逐格推进到 done（每格独立提交）
                if self._blocked(cyc):
                    return ids               # 格间阻断：不推阶段（在途轮保持游标，解除后续跑）
                try:
                    done = self.advance(cyc.cycle_id) == "done"
                except BudgetExhausted:
                    # 账本与 durable global_stop 已在 provider 的短事务中提交；本阶段业务产物尚未进入
                    # state.atomic。干净停在当前游标，绝不继续 retry/target，也不把在途轮误记 done。
                    self.last_stop_reason = "budget_exhausted"
                    return ids
                except CostAccountingFailed:
                    # 未知用量/落账失败的 durable global_stop 已提交；禁止重启后在同游标重发模型调用。
                    self.last_stop_reason = "cost_accounting_failed"
                    return ids
                except StageBlockedOnResources as e:
                    # 阶段等文件（CP8.5）：请求单已落——干净停止推进（在途轮保持游标，本阶段零提交），
                    # 下次 run_cycles 由 precheck 的 pending 文件请求阻断接管；resolve 后续跑重做本阶段
                    self.last_block_reason = str(e)
                    return ids
                self._publish_card(cyc.cycle_id)     # 阶段边界发布（§4.6.6）：每格提交后快照即时对人可见
                if done:
                    break
                cyc = self.state.cycle(cyc.cycle_id)   # 刷新游标（precheck 按 status 判 reasoning_start 到期）
            else:   # 进度护栏（codex NIT）：>8 格未到 done = 游标损坏（如 pc duplicate 而 status 未推进），fail loud
                raise RuntimeError(f"cycle {cyc.cycle_id} 推进 8 格未达 done——游标疑似损坏（status 未随阶段推进）")
            ids.append(cyc.cycle_id)
            if self.stop_controller is not None:  # §4.4.6 安全网：本轮完成后评估 τ 判据①②（每轮恰一次）
                hit = self.stop_controller.check_after_round()
                if hit is not None:
                    self.last_stop_reason = hit["reason"]
                    break                    # 自终止：落了 durable global_stop，下次 run_cycles 亦拒推进
        return ids

    def _blocked(self, cyc) -> bool:
        """跑 precheck（装了才跑）：消费到期 directive + 判阻断。拒因记 last_block_reason 供观测/上层日志。"""
        if self.precheck is None:
            return False
        self.last_block_reason = self.precheck(cyc)
        # A stage-boundary set_budget can lower the durable ceiling below money
        # already spent *after* run_cycles' round-start budget check.  Re-read
        # global_stop here so the same precheck cannot proceed to one extra
        # Runner call (and so resident mode exits instead of polling forever as
        # if this were a reversible pause).
        if self.stop_controller is not None:
            stopped = self.stop_controller.already_stopped()
            if stopped is not None:
                self.last_stop_reason = stopped
                self.last_block_reason = None
                return True
        return self.last_block_reason is not None

    def _publish_card(self, cycle_id: str) -> None:
        """阶段边界原子发布 status_card（装了 publisher 才发）。发布失败向上抛（fail loud，见
        SqliteStatusPublisher 注）——卡可重建，但静默丢发布会让人机窗口无声过期。"""
        if self.status_publisher is not None:
            self.status_publisher.publish(cycle_id)

    def _resume_or_open(self):
        """取本轮要推进的（已 setup 的）cycle；None = 应停机。恢复安全：先看在途轮（续跑其未竟阶段），
        无在途轮再据上轮 selection 决定开新轮还是停机。"""
        cyc = self.state.inflight_cycle()
        if cyc is not None and cyc.route is None and self.state.daemon.query_one(
                # 标记探测**无条件**（内联 ImportWorker.is_worker_cycle 同口径查询——不依赖 worker 已装配，
                # 内审 SHOULD：否则未装配时在途 worker 轮会被 _setup_cycle 误派研究 route = 静默损坏）
                "SELECT 1 FROM decision WHERE actor='orchestrator' AND type='import_worker_cycle' AND cycle_id=?",
                (int(cyc.cycle_id[1:]),)) is not None:
            if self.import_worker is None:
                raise RuntimeError(f"在途物化 worker 轮 {cyc.cycle_id} 但 import_worker 未装配——"
                                   "拒绝当研究轮处理（装配 ImportWorker 后重启续物化）")
            # 在途物化 worker 轮（OPEN #6 裁决④：route=NULL + 标记）→ 交物化 resumer 续跑
            self.import_worker.resume_cycle(cyc)
            cyc = self.state.inflight_cycle()    # worker 已终态 → 落回常规研究轮取位
        if cyc is None:
            prior = self.state.last_done_cycle()
            if prior is not None:
                if prior.next_intent == "terminate":
                    return None              # 上轮选择停机（持久标志）
                # **开新轮前**拒不支持的续轮（免落 route=None 死轮）：attack 须已装配 AttackStages（M4 CP5.4）。
                if prior.next_intent == "attack" and self.attack is None:
                    raise NotImplementedError("attack 续轮需装配 attack=AttackStages（M4 CP5.4）")
                if prior.next_intent not in ("decompose", "attack"):
                    raise NotImplementedError(f"未知续轮 intent: {prior.next_intent!r}")
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
            if prior.next_intent == "attack":
                # attack 起手 route='attack'（§2.3 规则5：route 在 plan 后特化——本实现 plan 只落 build 目标，
                # 起手即 attack；reuse_only/eval_only/dependency_wait 特化随对应 plan 形态接入）
                self.state.set_route(cyc.cycle_id, "attack")
                self.state.activate_question(prior.next_question_id)
                return
            if prior.next_intent != "decompose":                      # 防御（run_cycles 已前置拒）
                raise NotImplementedError(f"未知续轮 intent: {prior.next_intent!r}")
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
        if cyc.route == "attack":
            if self.attack is None:
                raise NotImplementedError("attack 轮需装配 attack=AttackStages（M4 CP5.4）")
            return self.attack.advance_stage(cyc)   # 按 cycle.status 游标推进一格（多格轮，run_cycles 内循环驱动）
        if cyc.route not in ("bootstrap", "decompose"):
            raise NotImplementedError(f"未支持的 route={cyc.route!r}（reuse_only/eval_only/dependency_wait 特化随对应 plan 形态接入）")
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
            # reasoning-only 轮（bootstrap/decompose）**不** persist-then-consume（不同 attack 轮）：非法
            # selection 抛 ValueError → 整 atomic 回滚（create_root 等一并复原，投影不漂移）→ 重启重调
            # provider（非确定，可复原）——这是本路径的既定恢复契约（test_advance_*_rollback 锁）。graceful
            # terminate 只用于 attack 轮的**持久化** reasoning（那里非法 selection 会确定性重崩=真楔死，CP8.8）。
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
