---
name: bundle-stage
description: 从当前 BundleStageRunRequest 与已接受 FormalPlan 滚动规划、执行并恢复 Target 工作。用于 Bundle Stage 需要形成 Target／依赖候选、复用实现、异步协调可恢复 TargetRun、消费完成或升级通知、收口 TargetCommit，或提出语义重规划与耗尽时。
---

# Bundle Stage

把 FormalPlan 中的证据缺口实现成可供 Reasoning 使用的正式测量闭包。由 Bundle 主 Agent 掌握整体策略、Target 分解和调度；由 TargetRun 根 Agent 负责局部闭环与 Monitor Loop；由各 State Owner 保留身份、资产、执行、接纳和 Stage 推进权。

执行前完整读取 [Bundle 合同](references/contract.md)；它是滚动策略、Target 粒度、复用、审查、监控、测量闭包与结果分支的唯一语义来源。

首次读取或写入 Owner 状态、请求 Target 工作、恢复 TargetRun 或提交 Stage 候选前，读取 [Owner 操作](references/owner-operations.md)。Target 的已冻结最小不变量与尚未解决的合同只在其中的 [Target 合同 seam](references/owner-operations.md#target-合同-seam) 维护。

主 Agent 可以按任务需要调用一个或多个子智能体。探索和审阅子智能体只返回 candidate、finding 与 provenance；正式 Target 使用可恢复的 TargetRun，其根 Agent 还可以继续派生子智能体。

正式 Target 工作通过异步请求交给独立、可恢复的 TargetRun 根 Session。Bundle 主 Session 发起后可以 wait、暂停或结束；wait／wake 只传递唤醒，不传递 Target 数据。TargetRun Monitor Loop 持有 raw logs、实时 metrics、snapshot、增量 observation、cursor、停止与恢复，并可派 monitor 子智能体。只有 Target 完成或确需 Bundle 级决策时，才通过 durable、coalesced、compact `TargetWorkNotice` 写入 `Bundle Inbox`。Bundle 被唤醒或由新 Session 接手后，重新读取权威 frontier、Inbox 与 currentness；不依赖父子 transcript，也不把 notice 当成 Owner receipt。

## 1. 锁定调用闭包

1. 取得类型化且当前有效的 BundleStageRunRequest、根 Execution Fence、不可变 ContextPack，以及 FormalPlan 的 canonical content hash 和直接绑定该 hash 的 current Owner 接纳回执。
2. 完整读取 EvidenceReuseSet、GapSet 和全部 gap ExperimentBrief，核对 Owner 回执、内容哈希、Quest／Cycle／Stage 绑定与当前性。
3. 把缺失、漂移或不可验证的身份、权限、回执、端口及当前性路由为类型化技术阻塞。
4. 当 FormalPlan 没有 gap ExperimentBrief 时不启动 Bundle，由 Advancement Engine 验证跳过依据。

完成标准：所有输入属于同一不可变调用闭包，每个 ExperimentBrief 都可追溯到精确 FormalPlan 内容绑定；同一 FormalPlanRef 下的内容改写不能沿用旧回执，本地草稿也没有被当成正式事实。

## 2. 建立滚动策略

1. 先形成足以启动首批工作的 Session-local 策略，再随已接受结果、Bundle Inbox 中的完成／升级通知、资源和 blocker 滚动修订；不要求预先冻结完整 Target DAG。
2. 从每个 ExperimentBrief 的冻结语义归一化待实现测量单元，并保存实际输入依赖、held-fixed 条件、候选复用路线、资源提示和 Target／结果覆盖关系；滚动策略结束时不得漏掉必需单元。
3. 把需要上游已接受结果的工作后置；让共享同一已接受锚点且互不消费新结果的工作并行。
4. 可以在正式 Target 前派生探索子智能体，但只把其输出当作可丢弃候选；在 Target 正式化前不产生受保护执行、正式测量或可复用资产副作用。

完成标准：首批工作已有可审计理由与输入边界，尚未规划的缺口仍显式可见，策略没有冒充正式 Target、DAG 或 frontier。

## 3. 复用并固定实现

1. 按 Bundle 合同从局部已接受上游向外扩展复用搜索，并根据实际适配度调整路线；不要把当前 Question 或本轮 Target 当成候选边界。
2. 优先绑定精确既有 Implementation Revision、VariantRun、CheckpointArtifact 或 Evaluation 实现；使用外部成熟实现时冻结来源、版本、选中内容、许可和补丁 provenance。把每份 source／version proof 通过内容绑定和接纳回执指向实际执行的 exact Implementation Revision；局部、相关历史或全局 Baseline Pool 资格还要由 eligible TargetCommit 锚定的内容绑定证据与绑定该证据 content hash 的 current receipt 证明，不接受 Target 自报层级。
3. 让比较中未声明变化的实现绑定同一份 held-fixed exact Implementation Revision。只有实验明确比较实现时，才把不同 revision 作为计划内变化。
4. 仅在没有合适实现、实现足够简单，或自行实现本身属于 SemanticDelta 时新写代码，并记录理由。

完成标准：每条候选路线都能说明复用了什么、为什么适用、实际改变什么，以及哪些精确实现必须保持不变；每层 disposition、正式 reason ref、source proof 与 greenfield 例外都进入 Research Graph 权威 Target spec 的完整内容绑定，并由直接绑定该 content hash 的 current receipt 证明，新 Bundle Session 不能用本地重算结果改写它；所选 source proof 已绑定实际 Implementation Revision，Owner 层级资格有可验证 TargetCommit 锚点而非自报标签。

## 4. 请求可恢复的 Target 工作

1. 以小批量或单个候选异步提交当前足够明确的 Target 工作，使用 Owner 操作中的 #63 seam 协调正式 identity、DAG、frontier 与 TargetRun；提案结果必须能读回权威 Target spec content binding 及其 current receipt，而不是只返回 TargetRef。
2. 发起 Target 工作时，在异步 launch request 中冻结精确 accepted inputs、权威且 current 的 Target spec binding／acceptance receipt 与 recoverable requirement。Owner／port admission 在建立 TargetRun 或产生其他执行副作用前原子验证这些条件，只返回 opaque operation／control ack；生产签名、issuer、传输与 lifecycle 仍由 #63 决定。TargetRun admission 可以先于代码差异形成，但此时只允许实现、静态检查与审查准备；使用某个 revision 的受保护训练／评估必须通过第 5 步代码审查门禁。
3. 让 Bundle 主 Agent 决定优先级、并发、资源与高层 control intent；让 TargetRun 根 Agent 负责单个 Target 的启动、监控、停止和恢复执行，并允许其调用自己的子智能体。确需改变 Bundle 策略或取得更高权限时，由 TargetRun 根 Agent 写入升级通知。
4. 把 Target、TargetRun 和 Agent Session 保持为三个身份。Target Agent 丢失时，由 TargetRun Monitor Loop 协调受信 Agent Runtime 恢复或替换执行者，不替换 Target，也不重造已接受资产。

完成标准：每个在途正式 Target 的 launch request 都冻结权威且 current 的完整 spec 内容绑定、精确 accepted inputs 与 recoverable requirement；admission 已在执行副作用前原子验证，Bundle 只收到 opaque operation／control ref。每个已建立 TargetRun 还有 durable Inbox subject，Monitor Loop 不依赖 Bundle Session 存活；尚未审查的 revision 没有进入训练／评估，Advancement Engine 没有被引入逐 Target 调度。

第 5、6 步组成 Target 局部循环，不是“一次执行完第 5 步才开始第 6 步”的线性阶段。先完成一轮整体实现／复用与自检，再通过一次候选就绪审查门禁，然后由 TargetRun 根 Session 启动安全执行并持续监控；只有代码修复形成新的完整候选 revision 时才重新进入代码审查，形成 terminal candidate 后再完成结果审阅与提交。

## 5. 完成 Target 局部闭环

1. 让 Target Agent 先完成一轮整体实现或复用，并跑完与该候选相关的静态检查和快速自检；普通编辑中间态不触发代码审查。
2. 非空代码差异形成完整候选 revision 后，让 TargetRun 根 Agent 新建一个独立代码审查子智能体，由该子智能体执行 `$code-review`；review scope 同时携带权威 Target spec 与 FormalPlan 的 exact content binding 及各自 exact current acceptance receipt。把这些证明连同 fixed base、diff、candidate content、完整复用 trace、输入与审查结果纳入同一个不可变 review-evidence payload，回执直接绑定该 payload 的 content hash。Reviewer Session 不得复用任何 TargetRun 根 Session identity，TargetRun 根 Agent 不在自己的实现上下文中兼任 reviewer；空差异记录 `not_applicable(empty_diff)`，不创建虚假 reviewer。
3. 让 Target Agent 逐条处置 Standards 与 Spec findings。需要修改时先合并完成这一轮全部修复与自检，形成新的完整候选 revision，再用 fresh review ref、reviewer Session 与 spawn evidence 新建独立代码审查子智能体复审；不要按文件、commit 或每个中间补丁频繁调用 `$code-review`。
4. 每个 preflight 重新读取权威 Target spec 与 FormalPlan 的 content binding／current receipt，并持有该 exact Implementation Revision 内容的 Owner 接纳回执；本地候选重算出相同或新的 hash 都不替代 Owner 证明。只有最新完整候选 revision 的内容已接纳且完整 scope 的代码审查已通过，才使用它进入受保护训练／评估。代码、依赖锁、来源 pin、补丁、Target spec 或 FormalPlan 内容变化会使旧审查失效；仅替换 Session、恢复 TargetRun 或重试相同已审查 revision 不触发重复代码审查。
5. 先按 [Bundle 合同的 Baseline／Variant／Evaluation 身份轴](references/contract.md#baselinevariant-与-evaluation-身份轴)对齐计划中的因果变化，再按[测量语义与原子聚合](references/contract.md#测量语义与原子聚合)划分 EvaluationAttempt。处理 seed、replicate、k-fold 或其他 Protocol 内部组成时，先取得 Owner seam 返回的 current accepted aggregation proof，再根据 FormalPlan 的原子接纳单元、精确 Variant／ProtocolVersion、固定聚合与是否改变后续状态决定保持一个 Attempt、拆分或 fail closed；不根据局部标签、数量或临时模型推断领域身份。
6. 提交前由 current TargetRun 根 Session 新建独立结果审阅者，检查执行绑定、必需 Metric、日志与资产 provenance；它与该 Target 的代码 reviewer 使用不同 Session／spawn evidence，只给 findings，不拥有接纳权。

单次迭代完成标准：整体代码候选和自检先完成；非空差异随后由独立子智能体完成一次 `$code-review`，最新完整 revision 通过门禁后进入安全执行，或形成需要第 6 步处理的观察。局部循环完成标准：当前 Target 的 terminal candidate 恰好选择一个 EvaluationAttempt 及其唯一 MetricResult，代码与结果 finding 均有 disposition，所有正式事实都有 Owner 回执；否则保持循环或明确阻塞。

## 6. 让 TargetRun 局部监控并恢复

1. 让 current TargetRun 根 Session 持有 Monitor Loop，并按需派生 monitor 子智能体。首次进入或恢复 Target 时读取有界 snapshot，随后只按 cursor 和状态 revision 读取增量事件；逐条验证 MonitorObservation、TechnicalBlocker、SemanticBarrier 与 closure 绑定 current TargetRun／Execution Attempt／Execution Fence，把 raw logs、实时 metrics、snapshot 与 cursor 留在该 TargetRun 的执行边界和外部追加式资产中。
2. 让 TargetRun Monitor Loop 只因控制／围栏失效、工程有效性风险或预注册停止规则中断执行。验证 stop decision 绑定精确 Target／TargetRun／Execution Attempt、受信 termination receipt 与进程树排空，并且每个 Execution Attempt 至多只有一份终止型 StopDecision；预注册规则还要绑定冻结 ProtocolVersion。指标差、零效果、不显著或否定假设本身不触发停止或修复。
3. 让 TargetRun 根 Session 在中断后先等待受信执行守护收口，再修改代码或输入；把每个正式 blocker 的完整身份登记为不可变，并且只消费一次 recovery receipt。从 TargetRun-local 冻结恢复包 resume；无法 resume 时按 #63 权威结果建立替代 Session、新 Execution Attempt 与新 Execution Fence。代码修复必须显式声明 replacement Implementation Revision，并先完成整批修复、自检、内容接纳和 fresh 独立子智能体 `$code-review` preflight，再允许 replacement 执行；按 recovery transition 的时间顺序保存这些 preflight。声明发生代码变化的 replacement content hash 必须不同于紧邻前一 preflight；相同内容只能按相同已审查 revision 的纯执行恢复处理。最终实现 provenance 精确保留初始复用来源，以及每个实际执行 revision 的 content binding 与接纳回执，不能让修复后的最终 revision 仍只声称初始来源 revision。未声明 replacement revision 的纯执行恢复只能沿用旧审查，不能静默换 revision 或生成新 preflight。永久 retired 旧身份，拒绝 recovery receipt 重放与 A→B→A 复活；若恢复使用后继 TargetRun，把退役前任与后继都纳入恢复证据闭包。不要仅因执行恢复推断新的 EvaluationAttempt。
4. 当 Target 形成 terminal candidate，或确需 Bundle 级策略、control intent、权限或 HumanRequest 时，写入 durable、coalesced、compact `TargetWorkNotice`。凡穿过 Bundle coordinator、Owner 或 Target port 的根投影，包括 control request 与 control ack，都使用闭合、有界值：字段符合 exact primitive／canonical record schema，所有字符串都是合法 UTF-8，完整嵌套投影同时受总字节与总节点预算约束；在 control 或其他副作用前完成验证，编码失败或预算超限统一 fail closed。TargetRun-local `MonitorObservation` 等类型不得嵌入 Bundle-facing 投影。Notice 只携带 subject-bound 权威 ref、单行紧凑原因、待处理义务与 durable handoff-manifest ref；manifest 使新 Bundle Session 能枚举最终报告所需的审查、恢复、retired identity 与 receipt refs。Bundle 级升级的内容绑定覆盖 blocker、reason、scope 与 obligations 全部紧凑 payload，正式 receipt 直接绑定该 content hash；任一字段变化都需新回执。Notice 不携带 raw stdout／stderr、实时 metrics、snapshot、monitor cursor、完整事件历史、transcript 或隐藏推理。
5. Bundle 可以等待 Inbox 唤醒，也可以暂停或结束当前 Session；wait／wake 本身不传数据。同一 generation 下没有新 notice 是正常的可恢复暂停，不是协议失败。被唤醒或由新 Session 接手后，在任何 proposal-capable Owner 操作之前先读取 durable Inbox；每个 Target 在发起前读取权威 frontier／currentness。若 Inbox 已有该 Target 的 durable notice 而 frontier 缺失，先 fail closed，绝不重新派发。消费 notice 时重新验证 terminal 类型、内容绑定的升级依据、reason／obligations、停止证明及其与 terminal 的状态关系、按顺序对应的恢复 revision↔fresh review、包含退役身份的恢复证据精确闭包与 handoff manifest；权威 frontier 的完整 `current_handle` 必须与 handoff 最终 handle 精确相等。完成验证后、收口前再次确认完整 frontier entry（含 revision、terminal fact 和 `current_handle` 全部字段）未变，再保存已接受 TargetCommit／测量资产并重算局部策略和 ready 工作。

完成标准：TargetRun Monitor Loop 内每条增量观察只消费一次，每个停止、修复、恢复和替代 Session 都有精确依据；Bundle-facing envelope 是闭合值，Bundle 从未接收实时 observation、日志、metrics 或 monitor cursor，并能在没有父子 transcript 的新 Session 中重建。代码修复形成新 revision 时回到第 5 步的完整候选门禁；相同已审查 revision 的纯执行恢复直接继续安全执行；形成 terminal candidate 时回到第 5 步完成独立结果审阅与候选提交，随后才写入完成通知。

## 7. 收口 Bundle 结果

1. 严格按 Bundle 合同逐 Target／ExperimentKey durable 收集 `realized | blocked | replan_required` 证据；首份 SemanticBarrier 只更新对应 remaining work，不越过仍在运行的并行 Target，也不提前形成 Bundle 级结果。
2. 保留已接受部分结果；继续处理语义内替代路线，把技术 blocker、pending submission、unknown outcome 与仍可执行路线保持为在途状态。
3. 只有全部 remaining ExperimentKey 与路线都已对账，并且没有 active、blocked、pending 或 unknown 工作时，才按合同的聚合优先级形成 Bundle 级 `replan_required` 候选；realized 交接与严格的 `ExhaustionProposal` 仍分别满足各自门禁。

完成标准：每个 ExperimentKey 都有 durable realized、remaining 或 semantic-barrier 解释；所有并行 Target、路线与外部操作都已在聚合前对账。技术失败没有伪装成重规划或耗尽，有效负结果没有伪装成失败，active、blocked、pending 或 unknown 工作没有被终态越过。

## 8. 交接

1. 返回精确 FormalPlan／StageRunRequest、逐 ExperimentKey 覆盖、TargetCommit、EvaluationAttempt、MetricResult、Execution Attempt／current Execution Fence、CheckpointArtifact、所选日志／分析、实现 provenance、历次有效 code-review preflight／结果审阅、stop decision 与全部必要回执；报告本体和所有嵌套 map／sequence／provenance 都是深不可变的 canonical value，不保留调用方可变别名。
2. 协调所有未知外部结果，保存未采用 Attempt 和诊断资产的真实历史，但只把明确选择的资产放入提交闭包。
3. 把候选交给获授权 Owner；由 Advancement Engine 验证当前 request 与所需回执并形成 StageCommit。

完成标准：主 Agent 能说明每个 gap、Target 和外部操作的去向；交接报告在交付后无法通过嵌套容器原地改写，不存在 `BundleSuccess`，也没有把执行完成、资产接纳、测量接纳、TargetCommit 接纳或 Stage 推进合并成一个事实。

运行确定性参考测试：

```bash
python -B vnext/skills/bundle-stage/scripts/test_bundle_stage_mvp.py
```
