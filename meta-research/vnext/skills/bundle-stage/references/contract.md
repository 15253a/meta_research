# Bundle Stage 合同

本文件是滚动策略、Target 粒度、实现复用、审查、TargetRun-local 监控、异步通知、测量闭包、恢复与 Bundle disposition 的语义来源。

## 目录

- [调用闭包](#调用闭包)
- [全局理解与滚动策略](#全局理解与滚动策略)
- [Target 与测量粒度](#target-与测量粒度)
- [Baseline、Variant 与 Evaluation 身份轴](#baselinevariant-与-evaluation-身份轴)
- [依赖与并行](#依赖与并行)
- [复用与实现比较](#复用与实现比较)
- [TargetRun 与 Agent 拓扑](#targetrun-与-agent-拓扑)
- [代码审查与结果审阅](#代码审查与结果审阅)
- [TargetRun-local 增量监控、通知与停止](#targetrun-local-增量监控通知与停止)
- [测量闭包与正式接纳](#测量闭包与正式接纳)
- [恢复、阻塞与语义结果](#恢复阻塞与语义结果)
- [交接](#交接)

## 调用闭包

```text
BundleStageInvocation
  stage_request_ref
  validated_runtime_binding_ref
  root_execution_fence_ref
  foreground_epoch_ref
  context_pack_ref + context_pack_hash
  accepted_formal_plan_ref + content_hash + receipts
  EvidenceReuseSet
  GapSet
  gap ExperimentBrief[1..N]
```

BundleStageRunRequest 冻结以上来源绑定与完成合同。Bundle 只消费 Research Graph 已接受且 current 的 FormalPlan：权威投影同时给出 FormalPlanRef、canonical content hash，以及直接绑定该 hash 的 current acceptance receipt。只绑定稳定 ref 的回执不证明内容；同一 FormalPlanRef 下任一 ExperimentBrief、冻结语义、完成 cell 或输入引用改写后，旧 hash／receipt 失效。本地 PlanDraft、未接受内容或漂移的 `latest` 引用不能启动工作。

没有 gap ExperimentBrief 时不建立 Bundle Run。Advancement Engine 根据 current FormalPlan 和 Owner 回执形成显式 `StageCommit(Skipped)`。

## 全局理解与滚动策略

Bundle 主 Agent 在首次派发前读取整个 FormalPlan、EvidenceReuseSet、GapSet 与全部 ExperimentBrief。它保存一份可修改、可丢弃的 Session-local 策略：

```text
RollingBundleStrategy
  formal_plan_ref
  unresolved ExperimentKeys
  accepted anchor closures
  local Target candidates
  dependency hypotheses
  ready-work hypotheses
  reuse and implementation options
  resource and Agent options
  accepted-result coverage
  typed blockers and semantic risks
```

该策略不是 Target、DAG、frontier、TargetRunRequest、receipt 或跨 Stage artifact。主 Agent 可以先形成足以启动首批工作的局部策略，再依据 Owner 接纳结果、Bundle Inbox 中的完成／升级通知、资源与反馈提出后续 Target；它不必在第一步列出全部未来路线。

主 Agent 从 Goal、Characteristics、BoundaryConstraints、SemanticDelta 与必需 Metric 中归一化每个 ExperimentKey 的 measurement completion cells。该投影不扩展 FormalPlan schema，也不预先冻结 Target DAG；它可以随已接受事实细化，但在声明策略完整前，必须证明每个冻结义务所需的独立 cell 恰由一个 Target 候选覆盖，不能让候选列表反向缩小完成合同。

策略修改只能改变未提交候选与内部调度。FormalPlan 的 Goal、Characteristics、BoundaryConstraints、SemanticDelta、ExperimentKey 与输入引用保持不可变；需要改变时进入 `replan_required` 候选。

正式 Target 前可以使用探索／advisory 子智能体评估实现、依赖、资源或风险。其输出只有 candidate、finding 与 provenance；只有正式 Target 工作才能产生受保护执行、正式测量和可复用 TargetCommit 副作用。

## Target 与测量粒度

一个 result-bearing Target 以形成一个可接纳的测量闭包为完成目标。成功 TargetCommit 恰好选择一个已经获得 Formal Measurement Acceptance 的 EvaluationAttempt，以及该 Attempt 唯一的 MetricResult。

Target Agent 对闭包负责，但不自我接纳，也不要求亲自重跑全部上游步骤。以下都是复用优先策略自然形成的工作，而非例外：

- 复用既有 VariantRun 与 CheckpointArtifact，只在新 ProtocolVersion 下形成 EvaluationAttempt；
- 复用完整成熟 Implementation Revision，不产生代码差异；
- 对没有 CheckpointArtifact 的规则算法或交互系统形成精确 VariantRun、Execution Input Binding 与 EvaluationAttempt。

一个 Target 在成功前可以经历技术 retry、替代 Session、失败 VariantRun、未接受 EvaluationAttempt 与候选修订；这些事实不自动进入最终 TargetCommit closure。

无独立测量闭包的共享实现、检索和工程准备保持为 Bundle／Target 内部 shared work，不单独冒充正式 result-bearing Target。

### Baseline、Variant 与 Evaluation 身份轴

按被接纳的因果语义分类，不按文件、代码路径、Agent、进程、seed／fold 标签或实现方式分类：

- **Baseline** 是静态 forward 合同相同的模型主体。进入 forward 的运算语义改变才形成新 Baseline；训练或评估方式、等价 Implementation Revision 变化不改变 Baseline。“对照组”是某次比较中的角色，不是判定 Baseline identity 的条件。
- **Variant** 隶属于一个 Baseline，冻结训练与模型侧推理配方。训练数据与划分、训练预处理／增强、优化、checkpoint 选择、early stopping 及模型侧必需推理预处理属于 Variant。纯评估数据、held-out 划分、评估预处理、Metric 与聚合属于 ProtocolVersion。
- **VariantRun** 是一份精确 Execution Input Binding 下的实际状态形成记录。同一 Variant 可以有多个 VariantRun；技术 retry、Session 替换或子智能体故障本身不建立新 VariantRun。
- **ProtocolVersion** 是评估数据、划分、评估预处理、必需 Metric、停止规则与聚合语义的不可变精确测量合同。
- **Evaluation** 是精确 `Variant × ProtocolVersion` 的稳定幂等对象。它表示“要测什么”，不是一次执行、一份结果或 latest／best Attempt。
- **EvaluationAttempt** 是某个 Evaluation 下一次按已接纳测量语义划分的实际测量。它精确绑定 VariantRun 及所用 CheckpointArtifact，Formal Measurement Acceptance 以它为原子并且只产生一份 MetricResult。

`Baseline Pool` 不是 Baseline identity 的另一个 Owner，而是从 eligible TargetCommit closure 重建的全局候选投影；它可同时索引 Baseline、Variant、Evaluation、Attempt 与精确资产闭包。

### 测量语义与原子聚合

一次语义独立的状态形成建立新 VariantRun；一次语义独立的实际测量建立新 EvaluationAttempt。FormalPlan 通过 Characteristics、BoundaryConstraints、SemanticDelta 与 measurement completion cells 表达预期的因果轴、原子接纳单元与必需结果；Bundle 把这些语义映射为候选 Target、VariantRun 与 EvaluationAttempt，Research Graph 才拥有正式 identity 与接纳。

seed、replicate、batch、fold、checkpoint、临时模型数量或物理并发本身不决定领域身份。只有同时满足以下条件的多执行单元才保持在同一 EvaluationAttempt：

- FormalPlan 把它们规划为同一原子接纳单元，并且它们属于同一个 Evaluation；
- 当前 ProtocolVersion 的已接纳原子聚合声明冻结完整 internal part set 的一份精确顺序；标准 10-fold 或预注册 multi-seed 可以使用其自然声明顺序；
- EvaluationAttempt closure 与该冻结顺序逐项一致且没有重复；并发完成顺序、容器遍历顺序或本地编号不能事后重排 parts；
- 当前 ProtocolVersion 选择一个已正式接纳、内容精确冻结的 exact aggregation rule；ProtocolVersion、ordered internal part set 与 rule 共同进入 Attempt-level 聚合内容绑定和 current receipt；
- 只有聚合 MetricResult 被正式接纳并作为该 Attempt 的必需结果；part-level 值只是协议内部内容或带 provenance 的 LogAsset／AnalysisAsset；
- 任一 part 的中间结果都不会自适应地改变后续训练、checkpoint、超参、停止、part 集、聚合方式或路线。

Owner seam 返回的 current accepted aggregation proof 必须证明上述原子聚合内容仍为 current。对训练 seed，若已接纳的状态形成合同把完整 seed set 定义为一个复合 VariantRun，其多个临时模型／CheckpointArtifact 可以由一个 EvaluationAttempt 按固定协议聚合；若每个 seed 已经形成可独立引用的 VariantRun，则不能把多个 VariantRun 事后拼成一个 Attempt。纯评估随机性的 seed 可以直接作为 ProtocolVersion 的 internal part。

下列任一语义成立时拆分为多个 EvaluationAttempt，并由 Bundle 依据独立测量闭包决定是否也需多个 result-bearing Target：

- part 要被独立消费、解释、接纳、复用或形成独立必需 MetricResult；
- part 的结果用于自适应选择训练、超参、checkpoint、停止、后续 part 或路线，或者其状态被带出原子协议；
- parts 属于不同 Variant 或 ProtocolVersion，或者未被同一 current accepted aggregation proof 完整覆盖；
- FormalPlan 本来就要求多个独立 measurement completion cell，而不是一个聚合 cell。

标准 k-fold 通常是一个聚合 cell、一个 EvaluationAttempt 和一份 MetricResult。预注册的 multi-seed mean／distributional summary 也可以使用同一结构。跨真正独立 Attempt 的汇总保存为有精确 provenance 的 AnalysisAsset；它不能把多个 Attempt 拼成一个虚构 Attempt，也不能替代任一必需 MetricResult。

## 依赖与并行

依赖表达实际输入消费，而不是时间顺序、共享目录、GPU 排队或“看起来相关”。常见但非固定的结构是：先完成一个锚点 Target，再让多个模块替换、超参或 Evaluation Target 共同复用该精确闭包并并行。

只有下游工作需要上游新产生的已接受资产或 TargetCommit 时，才建立真实依赖。共享同一已接受锚点、互不消费对方新结果的工作可以处于同一 ready frontier。

本地 dependency label 只用于表达滚动假设。下游进入受保护执行前，Research Graph 的权威 frontier／DAG 与 Execution Input Binding 必须逐项证明它消费了精确、current、accepted 的上游 TargetCommit／asset refs；用于自适应选择下游路线的 accepted result 也必须进入该依赖，不能只写成调度 gate。“先看到了上游完成事件”不等于真实输入依赖已经成立。

如果后续超参、协议、停止规则或实现选择取决于上游测量结果，则存在科学依赖，应等待该结果被正式接纳；不能用并发执行掩盖自适应选择。

正式 Target identity、spec、DAG、frontier、TargetCommit currentness 和依赖接纳由 Research Graph 拥有；Bundle 只提出候选并消费权威结果。精确操作统一经过 [Target 合同 seam](owner-operations.md#target-合同-seam)。

## 复用与实现比较

### 局部向外扩展

Bundle 默认按下列方向寻找实现和执行复用，主 Agent 可依据适配度、成本与风险调整具体下钻顺序：

1. 当前 Target 的精确已接受上游闭包，以及本轮已经冻结的共享实现；
2. 当前 FormalPlan／Question 相关的已接受历史闭包；
3. 全局 Baseline Pool 中当前 eligible、可验证的 TargetCommit closure；
4. 固定版本的成熟外部包、论文实现或 GitHub 源码；
5. 有明确理由的自行实现。

Baseline Pool 保持一个全局、只读、可重建的候选投影。Plan 以当前 Question／AnswerContract obligation 为查询入口，渐进选择 EvidenceRef 并识别 gap；Bundle 从局部向外发现实现与执行来源。Question 决定查询语义，不把候选语料限制在同一 Question。Pool eligibility 不是全局科学充分性或“最佳实现”事实。

对外部实现至少保存 canonical source、精确版本或 commit、实际选中路径／内容 hash、许可与 NOTICE 义务、复用方式、补丁 hash，以及采用或拒绝候选的理由。这些字段属于结构化 source proof；一旦选中，它们与验证 receipt 一起进入 implementation provenance，不能只写在搜索笔记中。热度、star 或 release 数只能帮助发现候选，不能替代适配判断和正式接纳。

每份 source proof 必须把 source ref、exact version、实际选中内容及适用的 license／patch 证据，通过不可变内容绑定指向一个 exact Implementation Revision，并提供该内容的 Owner 接纳 receipt。所选 source proof 绑定的 revision 必须就是候选和受保护执行实际使用的 Implementation Revision；只相等的 source／version 文本不能证明两个 Agent 实现了同一份内容。

`accepted-local`、`related-history` 与 `global-baseline-pool` 层级的 eligibility 是 Owner 事实：它必须锚定一个当前 eligible 的 accepted TargetCommit，并用内容绑定证据同时冻结 tier、TargetCommit 锚点、source／version、exact Implementation Revision 及其 content hash，再由直接绑定该 eligibility content hash 的 current receipt 证明。只绑定稳定 statement ref、却可在重算 hash 后沿用的 receipt 无效。Target／Bundle 自报的 tier 标签、source 自带的 pool 声明或未绑定 TargetCommit 的索引命中都不构成 eligibility；成熟外部或自行实现也不得借用这些 Owner 层级资格。

每个最终选择保存各候选层级的 `selected | rejected | not_found | not_applicable` disposition、精确 source refs、正式 reason refs、选择理由与跳过更近候选的理由。任何层级一旦携带 source proof，无论最终 selected 还是 rejected，都要验证精确版本、实现内容绑定与接纳 receipt；Owner 层级还要验证 TargetCommit 锚定的 eligibility 闭包。`not_found`／`not_applicable` 不得同时声称存在 source proof。完整 trace 与 greenfield 例外作为 Target candidate 的一部分，由 Research Graph 权威 Target spec 的 canonical content hash 冻结；直接绑定该 hash 的 current acceptance receipt 与 TargetRef 一起构成可恢复的 spec binding。Bundle 新 Session 必须从 Owner 投影或权威 frontier 重读该 binding，不得以当前 Session-local candidate 重算 hash 替代它；任一 tier、disposition、reason、source proof、eligibility 或 greenfield 例外改写都要形成新的 Owner 内容绑定与回执。完整 reason／source／receipt refs 进入 preflight／handoff，不能只持久化 selected source 后事后重写 rejected／not-found 理由。所选来源的 source ref、exact version ref、Implementation Revision 内容绑定、层级资格证据及相应 receipts 进入实现 provenance。直接自行实现时，必须已经逐层处置更近候选，或显式记录“实现足够简单／自行实现属于 SemanticDelta”例外。该 trace 证明复用优先原则被执行，但不冻结搜索算法、时间预算、候选数量或固定排名。

### held-fixed Implementation Revision

当实现不是计划内实验变量时，所有比较单元绑定同一份 held-fixed exact Implementation Revision。“语义相同”不足以允许不同 Agent 静默重写实现。

FormalPlan 通过 BoundaryConstraints 与 SemanticDelta 冻结必须保持不变的语义条件，但不要求预先挑选所有实现，也不新增 `held_fixed_slots` 生产字段。Bundle 从这些条件中归一化语义槽位，为每个槽位选择并记录 `semantic slot -> exact Implementation Revision`；同一比较内的所有 Target 必须逐槽位精确相等，不能用 revision 集合相等掩盖槽位交换。

当实验明确比较等价实现时，在 Characteristics／BoundaryConstraints 中声明该比较；每个实现使用不同的精确 Implementation Revision 与独立 Target，其他条件保持不变。等价实现 revision 的变化不自动创建 Baseline、Variant 或 ProtocolVersion；实际因果作用仍按 SemanticDelta 分类。

代码修复形成新的 Implementation Revision。若修复仍在允许语义内，则为新 revision 保存内容绑定与 Owner 接纳回执，把它一致应用到受影响比较单元并重新审查、重新测量；声明代码发生变化时，新 content hash 必须不同于紧邻前一 preflight，不能仅更换 revision／review identity 后复用旧内容。最终 provenance 同时保留初始复用来源和每个实际执行 revision 的内容闭包。若必须改变 FormalPlan 明确冻结的 exact revision 或其他 held-fixed 条件，则形成 `replan_required` 候选。

## TargetRun 与 Agent 拓扑

保持三个身份：

```text
Target        = Research Graph 的持久缺口工作身份
TargetRun     = Agent Runtime 的可恢复逻辑执行边界
Agent Session = Harness 的一次具体执行载体
```

正式 result-bearing Target 必须落在可恢复的 TargetRun 边界内。Bundle 主 Agent 在业务上异步调度它，但不把普通 child Session 或 Bundle 根 Session 的存活当成 Target 耐久性。建立 TargetRun 前，异步 launch request 冻结精确 accepted inputs、权威 Target spec content binding 及其 exact current acceptance receipt，以及 recoverable requirement；Owner／port admission 在创建 TargetRun、Session、frontier／monitor 状态或其他执行副作用前原子验证这些条件。TargetRun admission 允许根 Agent 先完成实现、静态检查与审查准备；它不授权尚未通过代码审查门禁的 revision 进入受保护训练或评估。

异步 Target 工作请求只向 Bundle 返回 closed、opaque operation／control ack。该 ack 不暴露尚未独立重读验证的 TargetRun handle；launch request／ack 的生产签名、issuer、传输与 lifecycle 仍由 #63 决定。Bundle 可以等待一次 Inbox wake，也可以暂停或结束当前 Session；wait／wake 只表示“应重读权威状态”，不携带日志、指标、snapshot、cursor、结果或 blocker 数据。TargetRun 根 Session 独立继续，直到形成完成候选或确需 Bundle 级决策。

TargetRun 根 Agent 可以使用 Harness 原生 spawn、fork、steer、interrupt、resume 与递归子智能体。它持续负责单个 Target 的实现、自检、审查 finding 处置、执行、`TargetRun Monitor Loop`、修复和候选提交；Monitor Loop 可以派生 monitor 子智能体。代码审查本身仍由根 Agent 在候选就绪门禁新建的独立子智能体执行。探索者、monitor、代码 reviewer 与结果 reviewer 都不取得 Target 身份或接纳权。

三个 canonical 术语保持以下边界：

- `TargetRun Monitor Loop`：current TargetRun 根 Session 内的实时执行闭环，独占 raw logs、实时 metrics、snapshot、增量 observation、event cursor、status revision、停止判断与恢复执行；
- `TargetWorkNotice`：Target 完成或确需升级时形成的 durable、coalesced、compact 通知信封，只携带 subject-bound 权威 ref、紧凑原因、未决义务和 durable handoff-manifest ref；它不是 TargetRun lifecycle state、Owner receipt 或接纳事实；
- `Bundle Inbox`：供 Bundle 唤醒或新 Session 重建时读取的 durable 通知投影；它只证明通知可读取，notice 内引用的领域与执行事实仍由相应 Owner 状态证明。

`TargetWorkNotice`、`Bundle Inbox` 与 handoff manifest 的生产 issuer、持久化 Owner、传输、确切 schema、receipt 和 lifecycle 不在本合同中冻结；这些仍经过 [Target 合同 seam](owner-operations.md#target-合同-seam)。

Target Agent 或 Session 故障不改变 Target。TargetRun Monitor Loop 优先协调受信 Agent Runtime resume 可恢复 Session；无法 resume 时先永久 fence 旧资格，再按 [Target 合同 seam](owner-operations.md#target-合同-seam) 返回的权威恢复结果建立替代 Session 与新 Execution Attempt。该结果可以继续当前 TargetRun，也可以引用获正式接纳的后继 TargetRun；Bundle 不自行推断或创建 TargetRun identity。执行恢复本身不创建 EvaluationAttempt。TargetRun-local 恢复包只提供给 resumed／replacement 根 Session，并且只包含：

- 精确 Target／ExperimentBrief 与已接受输入引用；
- current TargetRun、root Session、Execution Attempt、Execution Fence，以及正式 blocker／recovery receipts；
- Implementation Revision、Execution Input Binding 与代码审查状态；
- CheckpointArtifact、外部任务句柄、日志 cursor／状态 revision；
- Owner receipt、已协调副作用和未决义务。

该恢复包不进入 Bundle Inbox，也不伪造 transcript、隐藏推理或子智能体树。最终报告所需的历次 preflight／review、stop、recovery、retired identity、selected asset 与 receipt refs 必须另外形成 durable、Owner-verifiable handoff manifest；TargetWorkNotice 只携带该 manifest ref，不复制历史本体。旧 root Session、Execution Attempt 与 Execution Fence 一经永久 fence 就进入 retired 集合，任何后续 A→B→A 恢复都不能复活；若使用正式后继 TargetRun，旧 TargetRun 也 retired，而且恢复证据精确闭包同时保留退役前任与后继 TargetRun。Bundle 根 Session 替换后，从权威 frontier、Bundle Inbox、Owner currentness、handoff manifest 与已接受 TargetCommit 重建滚动策略和审计闭包，不读取 TargetRun observation stream，也不依赖父子 transcript。

Advancement Engine 只签发 Bundle StageRunRequest 并决定 StageCommit，不逐 Target 调度。哪些行为已经冻结、哪些仍等待 #63，只在 [Target 合同 seam](owner-operations.md#target-合同-seam) 列出；本合同不复制未决清单。

## 代码审查与结果审阅

### 代码审查

Target Agent 先完成一轮整体实现或复用，并跑完该候选所需的静态检查与快速自检。普通编辑中间态不是审查输入。非空差异形成完整候选 revision 后，TargetRun 根 Agent 必须新建一个独立代码审查子智能体，由该子智能体调用 `$code-review`；根 Agent 不能在自己的实现上下文中兼任 reviewer。该审查必须在使用该 revision 进入受保护训练／评估前完成。建立 TargetRun、编写实现和生成 diff 不受审查门禁阻塞：

```text
TargetExecutionPreflight
  target_ref
  target_run_ref
  candidate_revision_ref
  candidate_revision_content_hash_ref
  implementation_acceptance_receipt_ref -> candidate_revision_content_hash_ref
  candidate_ready_evidence_ref -> candidate_revision_ref
  self_check_evidence_refs[] -> candidate_revision_ref
  target_root_session_ref
  reviewer_child_session_ref
  reviewer_spawn_evidence_ref
  fixed_base_ref
  diff_ref
  review_scope
    candidate revision + content hash
    authoritative Target spec + content hash + exact current acceptance receipt
    accepted FormalPlan + content hash + exact current acceptance receipt
    ExperimentKeys + SemanticDelta values
    held-fixed exact Implementation Revision bindings
    accepted input refs + complete reuse-search trace refs
    repository standards refs
  review_evidence_content_hash -> complete review scope + review outcome
  review_evidence_receipt -> review_evidence_content_hash
```

该 preflight 只能在 TargetRun admission 之后、第一次受保护训练／评估 observation 之前形成。它把 candidate-ready、自检证据、Implementation Revision 内容接纳回执和完整 review scope 逐项绑定同一 Implementation Revision，并证明 reviewer child 的 parent 是该 TargetRun 的实际根 Session。review scope 必须携带权威 Target spec 与已接受 FormalPlan 的 exact content binding，以及分别直接绑定这些 content hash 的 exact current acceptance receipt。review evidence 的 canonical payload 覆盖这些 binding／receipt、fixed base、diff、candidate content、ExperimentKeys／SemanticDelta、held-fixed bindings、accepted inputs、完整 reuse trace、repository standards、reviewer 身份与 Standards／Spec findings 及 disposition；它的内容回执直接绑定完整 payload hash。只有 review ref、reviewer Session 或未进入 payload 的 receipt 字符串不证明审查过该 scope；任一 binding、receipt、scope 或 finding 变化都使旧 review evidence 失效。同一 Target、FormalPlan 或 Implementation Revision 的 content binding 一经观察就在整个 Bundle 内保持不可变；它们的接纳回执直接绑定各自 content hash。保留 Harness 的 parent／child Session evidence，统一 Session-role 账本禁止 reviewer Session 与任何 TargetRun 根 Session 共用 identity。Reviewer 子智能体只报告，不修改候选，也不签发 Implementation Revision、TargetCommit 或其他 Owner receipt；Target Agent 逐条形成 `revised | not_adopted` disposition。

一次 `$code-review` 对应一个已经整体完成并自检过的候选 revision，不按文件、commit 或每个编辑补丁重复调用。若 findings 或后续工程修复要求修改，Target Agent 先成批完成本轮修改与自检，形成新的完整候选 revision，再新建独立 reviewer 子智能体复审；新 revision 不得复用旧 `review_ref`、reviewer Session 或 spawn evidence，reviewer Session 也不得借用另一 Target 的根 Session identity。代码修复产生的 preflight 必须按 recovery transition 的时间顺序进入 handoff；声明代码变化的 preflight 不得复用紧邻前一 preflight 的 content hash，不能只靠“每个 revision 都能找到某个 Session”后把 tuple 最后一项冒充 current。任何代码、lockfile、来源 pin、补丁或候选 tree 变化都会使旧 review 失效；仅替换 Session、恢复 TargetRun 或重试精确相同的已审查 revision 不会使 review 失效。

当精确复用实现而代码 diff 为空时，记录 `not_applicable(empty_diff)`，同时保留来源、许可、binding 与执行验证，不为满足形式而创建 reviewer 子智能体。

### 结果审阅

EvaluationAttempt 已形成不可变结果候选、但尚未提交 TargetCommit candidate 时，调用新的独立结果 reviewer。它只检查必需 Metric 覆盖、协议与执行绑定、artifact／日志 provenance、异常与候选闭包，不重新解释科学价值。

结果 reviewer 必须由 terminal candidate 所属 current TargetRun 根 Session 新建，并与该 Target 的所有代码 reviewer 使用不同的干净 Session 与 spawn evidence；其 Session identity 也不能与任何 TargetRun 根 Session 重合。结果审阅记录必须绑定精确 EvaluationAttempt、MetricResult 与 asset manifest，保留独立 reviewer Session、parent／child spawn evidence、review ref 和 finding disposition；根 Agent 自报的完成布尔值不能替代该记录。每条 finding 由 Target Agent 处置；审阅意见不能替代 Formal Measurement Acceptance 或 TargetCommit acceptance。

## TargetRun-local 增量监控、通知与停止

`TargetRun Monitor Loop` 完全属于 current TargetRun 根 Session，并可把读取工作派给 monitor 子智能体。首次进入或恢复 Target 时读取最近的有界 snapshot；常规监控只消费 `after_cursor` 与 `after_status_revision` 之后的增量事件。每个 MonitorObservation、TechnicalBlocker、SemanticBarrier 与结果 closure 都必须绑定当前 handle 的精确 Target／TargetRun／Execution Attempt／Execution Fence；恢复后的旧 observation 不得跨过新 snapshot 或驱动当前结果。具体批量上限、退避和轮询节奏属于 TargetRun monitor 实现策略，不是 Bundle 合同或统计 interim-look 计划。

raw stdout／stderr、实时 metrics、snapshot、event cursor、status revision 与完整日志历史留在 TargetRun 执行边界和文件侧 append-only 资产中。TargetRun 的持久恢复状态可以保存 cursor、紧凑摘要、typed blocker、terminal observation 与正式 asset ref；Bundle 根 Context 和 `TargetWorkNotice` 均不接收这些流或 cursor。

凡穿过 Bundle coordinator、Owner 或 Target port 的 Bundle-facing 根投影（包括 planner update、Target binding／launch ack、control request／ack、frontier、notice／Inbox batch／handoff 与最终 report）都使用字段级 exact schema：每个 record 与值都是所声明的 canonical tuple／record／精确 primitive，不用子类、自定义 carrier 或跨类型 truthy 值替代 `bool`／`int`／`str`。所有字符串必须能严格编码为 UTF-8；每份完整嵌套根投影同时受总序列化字节预算与总节点／item 预算约束，不能以“单个字符串和每层 tuple 各自未超限”绕过总量边界。验证在 launch、control 或其他相应副作用前完成；UTF-8 编码失败、整数范围错误或预算超限统一 fail closed，不抛出未分类运行时异常。具体预算属于生产实现或 fixture 策略，不在本合同固定。`MonitorObservation` 是 TargetRun-local 记录；即使嵌入某个表面类型正确的字段，也不能进入 Target binding、`TargetWorkNotice`、`Bundle Inbox`、frontier、handoff 或其他 Bundle-facing 闭包。

停止判定只有三类：

| 观察 | 动作 |
| --- | --- |
| request／currentness／fence／授权失效 | 立即 fail closed 并请求受信终止 |
| 崩溃、OOM、NaN／Inf、错误输入、污染、缺产物或其他工程有效性风险 | 请求受信中断和排空；修复后以新 revision／binding 恢复 |
| 预注册 early stopping／futility 规则命中 | 按冻结合同正常停止并保存 stop receipt 与必需结果 |

合法名称不是停止权。TargetRun Monitor Loop 检测并处置停止条件，Agent Runtime／受信执行守护执行 fence、终止、排空和资源释放；TargetRun 的领域 lifecycle／successor 后果仍由 #63 权威合同决定。Monitor Loop 验证每个 stop observation 携带 subject-bound、current 的 stop decision／termination receipt，冻结精确 Target、TargetRun 与 Execution Attempt，并证明受信执行守护已经排空进程树；本合同不命名 stop decision 的 production issuer 或 transport。一个精确 Target／TargetRun／Execution Attempt 至多只有一份终止型 StopDecision；换用另一个 decision／receipt identity 不能为同一 Attempt 创建第二个终止事实。同一 decision／receipt identity 不得跨执行主体改绑，恢复后也不能把旧 stop identity 套到新执行上。`control_invalid` 立即 fail closed；`engineering_anomaly` 只允许进入受信终止后的 repair／recovery 或 typed blocker，不能直接接纳随后到达的 closure；`preregistered_rule` 还必须绑定冻结 StopRule 与精确 ProtocolVersion，最终结果必须来自该 ProtocolVersion 和同一 Execution Attempt。

指标下降、零效果、不显著、负结果或曲线不符合预期本身不允许提前停止、调参、改协议或跳过后代。用于调参、early stopping 或状态选择的数据进入 Variant 因果边界；纯测量流上的 sequential rule 必须冻结在 ProtocolVersion 中。

受信执行守护确认进程树排空前，TargetRun 根 Session 不修改代码或复用资源。停止、repair、restart、每次 look、未接受 Attempt 与负结果都保留可审计引用。

Target 完成或确需 Bundle 级策略、control intent、权限或 HumanRequest 时，形成一份 durable、coalesced、compact `TargetWorkNotice` 并使其可从 `Bundle Inbox` 读取。Notice 必须是闭合、有界的值投影，只指向权威 closure、blocker、request、receipt、reconciliation 与 durable handoff-manifest ref；每个 ref、reason／obligations 集合、单份序列化 notice、一次 Inbox batch 与 Bundle 实际读取的完整 handoff 投影都分别有界。reason 是有界单行摘要，obligations 是有界、无重复的正式 ref，不允许未声明字段，也不携带 raw stdout／stderr、实时 metrics、snapshot、monitor cursor、完整事件历史、transcript 或隐藏推理。Bundle 可以等待 wake，但 wake 不携带 notice payload；同一 generation 下没有新 notice 只是正常的可恢复暂停，不是协议失败。Bundle 在 wake 或新 Session 恢复后、任何 proposal-capable 操作之前先读 durable Inbox；Target 发起前读取权威 frontier／currentness。若 Inbox 已有该 Target 的 durable notice 而 frontier 缺失，Bundle 必须在产生异步派发副作用前 fail closed。Bundle 只在权威 frontier 的完整 `current_handle` 与 handoff 最终 handle 所有字段精确相等时消费 terminal handoff；收口前再次读取并确认完整 frontier entry（包括 revision、terminal fact 与 `current_handle` 全字段）未变。具体大小值属于生产实现或 fixture 策略，不在本合同冻结。

Bundle 不能只信 Target-local producer 已做过验证。它重新验证 notice 与 terminal 的 kind／fact／reason／obligations 双向一致；Bundle-level TechnicalBlocker 的 canonical escalation payload 覆盖 blocker、reason、scope 与 obligations 全部字段，内容绑定冻结该 payload，正式 receipt 直接绑定 payload content hash。绑定稳定 evidence ref 或重算 hash 后沿用旧 receipt 都无效；任一 scope／reason／obligation 变化都需新内容证据与回执。Bundle 逐条重验 StopDecision，并重放停止到 terminal 的状态关系：engineering stop 不得直达结果 closure，preregistered stop 的结果必须保持同一 ProtocolVersion 与 Execution Attempt，重复 stop record 也拒绝。每个 recovery transition 的 replacement revision 与 fresh review preflight 按时间顺序一一对应；recovery evidence refs 是包含退役／后继 TargetRun、Session、Attempt、Fence、blocker 与 receipt 的精确闭包。纯 Target-local blocker 不进入 Inbox；未经再次验证的 handoff 不形成 Bundle disposition。

## 测量闭包与正式接纳

每个成功 TargetCommit 冻结：

```text
Target
  -> Baseline
  -> Variant
  -> VariantRun
  -> Evaluation = Variant x ProtocolVersion
  -> selected EvaluationAttempt
  -> unique MetricResult
```

闭包还包括所选 EvaluationAttempt 实际使用的 `0..N CheckpointArtifact`、VariantRun 与 EvaluationAttempt 的两份 Execution Input Binding、当前 Execution Attempt／Execution Fence、实际变化的 Implementation Revision、逐语义槽位的 held-fixed exact Implementation Revision bindings、完整因果输入，以及本次明确选择的 LogAsset／AnalysisAsset。closure 的实现 provenance 精确等于已验证初始 source proof，加上历次实际执行 preflight 的 Implementation Revision、content hash 与内容接纳 receipt；代码修复后的最终 revision 不能只继承初始 revision 的 provenance。closure 必须通过当前 TargetRun handle 的精确 Execution Attempt 与 current 根 Execution Fence 提交；恢复后旧 fence 的迟到结果无效。两份 Execution Input Binding 必须各有唯一 identity 与唯一 acceptance receipt；不能让同一不可变 binding 或 receipt 同时绑定 VariantRun 和 EvaluationAttempt。在整个 Bundle 内再次出现同一 binding identity 时，其 subject、完整 inputs 与 receipt 必须逐项相同；同一 receipt identity 不能改绑另一 binding。所有实现 provenance 条目都必须来自结构化 source proof 或已验证 revision preflight；仅有正式外观但没有 proof 的 ref、本地路径或草稿都不能附加到合法集合后带入交接。RM asset receipt 必须绑定精确 accepted asset manifest，AR execution receipt 必须绑定精确 Execution Attempt，RG Formal Measurement Acceptance receipt 必须绑定精确 EvaluationAttempt，RG TargetCommit receipt 必须绑定精确 TargetCommit；任何 Owner receipt identity 在 Bundle 内再次出现时都必须保持同一 subject，合法前缀但 subject 不同的 receipt 无效。

Formal Measurement Acceptance 以 EvaluationAttempt 为原子。必需 Metric 完整时，Research Graph 才为该 Attempt 创建唯一 MetricResult；未接受 Attempt 没有 MetricResult。跨 Attempt 叶子不能共同补足一次 acceptance。

成功 TargetCommit 只选择一份 accepted Attempt，不把它变成 Evaluation 的 canonical、latest 或 best Attempt。未采用 Attempt、审查过程版本、raw log 与其他运行文件不自动进入 closure；它们仍按原生身份保留诊断历史。

TargetRun 根 Agent 必须对账本次工作实际产生的 Implementation Revision、VariantRun、CheckpointArtifact、EvaluationAttempt、LogAsset 与 AnalysisAsset。可复用或合同要求的正式产物按原生 Owner 路由提交入库，失败或诊断产物也保留真实 provenance；“产物入库”不等于把所有产物都选入 TargetCommit closure。

Target Agent 负责形成并提交候选；Research Memory 接受内容，Research Graph 接受身份、角色、Formal Measurement 与 TargetCommit，Agent Runtime 证明执行，Advancement Engine 推进 Stage。永久保持：

```text
execution completed
!= asset accepted
!= formal measurement accepted
!= TargetCommit accepted
!= Stage advanced
```

## 恢复、阻塞与语义结果

### 技术恢复

代码、依赖、Provider、资源、host、缺 Metric、receipt 不可验证、external outcome unknown 和可修复 Owner rejection 都保持为原生技术／反馈状态。TargetRun Monitor Loop 持有停止与恢复执行；TechnicalBlocker 必须有正式 blocker identity、subject-bound current blocker receipt，同一 blocker identity 的 Target、TargetRun、Execution Attempt、Execution Fence、原因、恢复资格与回执一经观察就不可改绑。声称可恢复时还要有 AR recovery receipt、旧资格已 fence 与完整 TargetRun-local recovery pack；一份 recovery receipt 只授权一次恢复 transition，不能在后继执行上重放，也不能让报告把多次 transition 折叠成同一证据。若修复改变代码，blocker／恢复包必须显式声明新的 Implementation Revision，replacement Session 必须在任何新受保护 observation 前形成绑定该 revision 的 content hash／acceptance receipt、candidate-ready、自检与独立代码审查 preflight；多次修复的 preflight 保持恢复顺序。未声明 replacement revision 的纯执行恢复只能沿用既有 preflight，既不能静默换 revision，也不能生成新 preflight。恢复证据在最终 disposition 为 `realized` 时也不能丢失，正式后继 TargetRun 不能挤掉退役前任的证据。在冻结语义不变时，由 TargetRun 根 Session 修复、等待、resume 或提交新候选；只有确需 Bundle 级决策，并且升级 payload 获内容绑定证据与正式 receipt 时，才通过 TargetWorkNotice 升级。失败次数、耗时或成本不触发重规划或耗尽。

能证明故障隔离时，只阻塞受影响 Target 与真实下游；隔离未知、共享环境污染或结果可信度受损时，阻塞更大范围。独立分支与已接受 TargetCommit 保持有效。

### disposition

| disposition | 充分条件 |
| --- | --- |
| `realized` | ExperimentBrief 所需测量单元均有 current、accepted TargetCommit closure；结果方向不限。 |
| `blocked` | 技术、权限、资源、回执、currentness 或 external outcome unknown 阻止可信继续；它不是 Stage outcome。 |
| `replan_required` | 已接受部分结果已保存，且所有剩余有效路线必然改变冻结 Goal、Characteristics、BoundaryConstraints、SemanticDelta 或 held-fixed exact Implementation Revision。 |
| `ExhaustionProposal` | 冻结合同内已无实质不同的可接纳候选，且所有提交、副作用、HumanRequest、technical blocker、unknown outcome 与 accepted-unconsumed result 均已协调为空。 |

先把每个 Target 形成的 SemanticBarrier 作为 subject-bound durable evidence 收集到对应 ExperimentKey 与 remaining route；单个 Target 的首份 barrier 只是局部事实，不会关闭其他并行 Target，也不形成 Bundle 级 `replan_required`。对单个 ExperimentKey，`replan_required` 的“所有剩余有效路线”只量化该 key 的 remaining work；只有该 key 的所有 remaining routes 都已对账，才能形成该 key 的分类。Bundle 继续协调其他 key，直到全部 remaining key／route 都有 durable disposition。

Bundle 级候选按以下规则聚合：

- 所有 ExperimentKey 都 `realized`，才能形成 realized 交接；
- 每个 ExperimentKey 都已 `realized` 或满足 `replan_required`、至少一个需要重规划，并且所有 Target 工作、remaining routes、提交与外部 operation 都已对账，才能形成 Bundle 级 `replan_required` 候选；
- 任何 active Target、`blocked`、`outcome_unknown`、pending submission、未收齐的 barrier disposition 或仍可执行路线都使 Bundle 保持在途；
- `ExhaustionProposal` 只用于冻结合同内没有可接纳候选、也没有可由改变冻结语义解除的 semantic barrier；若改变 FormalPlan 语义能打开有效路线，优先归为 `replan_required`。

Semantic barrier 必须覆盖每条已声明的 remaining route，并为每条路线提交结构化 disposition：ExperimentKey coverage、`requires_frozen_change` outcome、冻结字段、证据，以及逐 external operation 的 reconciliation outcome／receipt。同一 operation 出现在多条路线时只能有一个一致的终态 outcome／receipt；同一 receipt identity 不能绑定两个 operation。Bundle 报告保留这些 disposition 与 reconciliation refs。依赖安装失败、资源不足、单路线失败、`outcome_unknown`，或把技术 blocker 的标签改写成 `SemanticDelta` 都不能满足该证明。

一个实现路线失败但仍有保持语义的替代路线时继续探索。一个有效负、零、不显著、否定或不确定测量是 `realized`，由 Reasoning 解释。`replan_required` 与 `ExhaustionProposal` 都是候选；相应 Owner 保留正式判断。

## 交接

### TargetRun 到 Bundle

TargetRun 的实时执行边界只以 `TargetWorkNotice` 向 Bundle 交接完成或升级需要。Notice 进入 durable `Bundle Inbox`，可被 coalesce，但不得跨不同 Target subject 改绑权威 ref；它以 handoff-manifest ref 使完整审查与恢复历史可枚举，同时保持自身紧凑。它是闭合的唤醒与索引，不是 TargetRun lifecycle state、结果接纳或 Stage 事实。Bundle wait／wake 不传数据；Bundle 被唤醒或由新 Session 接手后，先读取 Inbox，再重新读取权威 frontier／currentness、handoff manifest 与 Notice 所指 Owner 状态，并在收口前重确认 frontier 没有竞态漂移。

### Bundle 到 Owner

Bundle 输出一份紧凑候选报告。报告是深不可变的 canonical value：外层 record、所有嵌套 map／sequence／set 和 provenance 值都使用不可变表示或防御性冻结副本，不保留生成器或调用方可原地修改的别名。交付后对原容器的修改不能改变报告内容或已验证 provenance。报告至少包含：

- 精确 StageRunRequest、FormalPlan 与 ContextPack refs；
- 每个 ExperimentKey 的 required cells、accepted TargetCommit refs、remaining work 或 semantic barrier；
- 所选 EvaluationAttempt、MetricResult、Execution Attempt／current Execution Fence、CheckpointArtifact、LogAsset ref、AnalysisAsset ref 与完整 provenance closure；不包含 raw log／metric stream、snapshot、monitor cursor 或 transcript；
- 每次实际 preflight 的 Implementation Revision、content binding／acceptance receipt、candidate-ready／self-check／review scope、reviewer identity 与 code review record，包括被后续修订替代的已执行 revision；另保存实现复用来源及其 license／content hash／patch proof、held-fixed slot bindings 和 result review record；
- typed blocker identity／receipt、retired 与 replacement execution refs、recovery、replan 或 exhaustion evidence；
- 所有需要验证的 RM／RG／AR receipt、stop decision／termination receipt 与 external operation identity。

Bundle 不创建 `BundleSuccess`。只有 Advancement Engine 可以在 current StageRunRequest、execution receipt 和全部必需 Owner receipt 均可验证后形成 StageCommit。
