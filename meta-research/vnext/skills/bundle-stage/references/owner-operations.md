# Bundle Stage Owner 操作

在读取正式状态、异步请求 Target 工作、消费完成／升级通知、恢复 TargetRun、提交测量／TargetCommit 候选、上报阻塞或提出 Stage 结果前使用本文件。以下名称表达语义调用，不冻结生产签名、issuer、传输或 lifecycle。

## 目录

- [权限](#权限)
- [已解析的语义调用](#已解析的语义调用)
- [Target 合同 seam](#target-合同-seam)
- [Harness 映射](#harness-映射)
- [原生结果](#原生结果)
- [监控与终止路由](#监控与终止路由)
- [Fixture 纪律](#fixture-纪律)

## 权限

- Advancement Engine：拥有 Bundle StageRunRequest、Foreground Epoch、StageCommit 与 ExhaustionProposal 的机械验证；不逐 Target 调度。
- Research Graph：拥有 FormalPlan、Target identity／spec／DAG／frontier、Baseline／Variant／Evaluation 身份与角色、Formal Measurement Acceptance、TargetCommit 及领域 currentness。
- Research Memory：拥有不可变内容、AssetVersion／MemoryRef、custody、integrity、availability 与 asset receipt。
- Agent Runtime：拥有 Run／Execution Attempt／Agent Session、Execution Fence、Capability／Resource Binding、执行级恢复／终止和 execution receipt；TargetRun 的领域 lifecycle、successor 与 cancellation 仍由 #63 决定。
- Bundle 主 Agent：拥有 Session 内整体策略、Target 候选分解、依赖／并行建议、复用与实现路线、优先级、资源／Agent 协调、control intent、滚动修订和结果收口；它只消费 Bundle Inbox 中的完成／升级通知，不持有 TargetRun Monitor Loop，也不是 State Owner。
- TargetRun 根 Agent：拥有单个 Target 内的实现、自检、审查 finding 处置、受控执行、`TargetRun Monitor Loop`、工程修复与候选提交；它可以派 monitor 子智能体，但不能在实现上下文中兼任代码 reviewer，也不能自我接纳 TargetCommit 或推进 Stage。
- Agent Harness：拥有原生 Session tree、spawn／fork／steer／interrupt／resume、工具循环、上下文与事件流；TargetRun 实时事件流留在相应根 Session／monitor child 边界内。

探索、代码审查和结果审阅子智能体只接收分支所需的最小冻结输入，返回 candidate、finding 与 provenance。非空代码候选整体完成并自检后，TargetRun 根 Agent 必须新建独立代码审查子智能体执行 `$code-review`；保留 durable parent／child spawn evidence，不能用根 Agent 的自审代替，也不能把 transcript 或隐藏推理当成恢复与接纳依据。每次正式提交必须使用相应 Run 的 current 根 Execution Fence，并由目标 Owner 签发回执。

## 已解析的语义调用

```text
observe_bundle_stage_run(...)
  TODO-IMPL(advancement_engine.observe_bundle_stage_run; source=#58)

observe_bundle_run_binding(...)
  TODO-IMPL(agent_runtime.observe_bundle_run_binding; source=#71)

verify_delivered_context_pack(...)
  TODO-IMPL(agent_runtime.verify_delivered_context_pack; source=#71)

read_formal_plan(...)
  TODO-IMPL(research_graph.read_formal_plan; source=#62)

verify_reuse_inputs(...)
  TODO-IMPL(research_graph.verify_reuse_roles_and_currentness; source=#57,#88,#98)
  TODO-IMPL(research_memory.verify_reuse_assets; source=#66)

accept_implementation_content(...)
  TODO-IMPL(research_memory.accept_implementation_content; source=#66,#88)

submit_implementation_roles(...)
  TODO-IMPL(research_graph.submit_implementation_roles; source=#88,#98)

submit_execution_input_binding(...)
  TODO-IMPL(research_graph.submit_execution_input_binding; source=#88,#98)

accept_result_assets(...)
  TODO-IMPL(research_memory.accept_result_assets; source=#66,#88)

submit_measurement_roles(...)
  TODO-IMPL(research_graph.submit_measurement_roles; source=#88,#98)

submit_formal_measurement_acceptance(...)
  TODO-IMPL(research_graph.submit_formal_measurement_acceptance; source=#88)

observe_run(...)
  TODO-IMPL(agent_runtime.observe_run; source=#71)

transact_run(...)
  TODO-IMPL(agent_runtime.transact_run; source=#71)

report_execution_blocker(...)
  TODO-IMPL(agent_runtime.report_execution_blocker; source=#90)

propose_bundle_stage_commit(...)
  TODO-IMPL(advancement_engine.propose_bundle_stage_commit; source=#58,#100)

submit_exhaustion_proposal(...)
  TODO-IMPL(advancement_engine.submit_exhaustion_proposal; source=#90)

reconcile_exhaustion_proposal(...)
  TODO-IMPL(advancement_engine.reconcile_exhaustion_proposal; source=#90)
```

这些调用只投影已经决定的语义。`read_formal_plan(...)` 必须返回 FormalPlanRef、canonical content hash 与直接绑定该 hash 的 current Research Graph acceptance receipt；只绑定 ref 的回执不是内容接纳。真实 Adapter、持久化、receipt 传输和外部副作用仍是生产 `TODO-IMPL`。

`observe_run(...)` 在观察 TargetRun 时只供 current TargetRun 根 Session／monitor child 使用；它不授权 Bundle 主 Session 读取 TargetRun event stream。Bundle 只观察自己的 Bundle Run binding，并通过下节的权威 frontier／Inbox 投影协调 Target。

## Target 合同 seam

Bundle 需要以下行为，但不据其名称推断未决 Owner、签发者、identity、基数、生命周期或传输：

```text
propose_targets(rolling_strategy_slice, experiment_coverage, dependency_candidates)
request_target_work(target_ref, frozen_inputs, recovery_requirements)
read_target_frontier(...)
read_bundle_inbox(...)
control_target_work(target_ref_or_handle, intent)
submit_target_commit_candidate(target_ref, measurement_closure)
reconcile_target_submission(operation_identity)
```

`request_target_work(...)` 是异步语义请求：建立 TargetRun 前，它冻结精确 accepted inputs、权威 Target spec content binding 及其 exact current acceptance receipt，以及 recoverable requirement。Owner／port admission 原子验证完整 request 和执行资格后，才创建 TargetRun、Session、frontier／monitor 状态或其他执行副作用；响应只返回 closed、opaque operation／control ack，不携带未经独立重读验证的 TargetRun handle。`read_bundle_inbox(...)` 只读取 durable、coalesced、compact `TargetWorkNotice`。所有穿过 Bundle coordinator、Owner 或 Target port 的根投影，包括 control request 与 control ack，都必须通过字段级 exact schema 验证，拒绝未声明字段、非 canonical tuple／record／primitive carrier 与跨类型 truthy 值。每个字符串严格 UTF-8 可编码，每份完整嵌套根投影同时有总序列化字节预算与总节点／item 预算；不能只限制单个 ref、子集合或每层 tuple。这些边界在 proposal、launch、control、submission 等副作用前验证，编码失败、整数范围错误或预算超限统一 fail closed。TargetRun-local snapshot／incremental observation／`MonitorObservation` 与 cursor 协议不是 Bundle-facing 调用，它们不能嵌入其他 Bundle 字段；其具体形状仍留在 #63 seam。以上名称、launch request／ack 的生产签名、fixture 中的具体预算以及 handoff manifest 都不规定生产 API、issuer、持久化 Owner、transport、schema、receipt 或 lifecycle；这些仍由 #63 决定。

### 已冻结最小不变量

- Bundle 可以滚动提出 Target／依赖候选并读取权威 frontier；
- Target 候选获得正式 TargetRef 时，Research Graph 权威 spec binding 同时冻结完整 candidate 与 reuse trace，并有直接绑定 spec content hash 的 current acceptance receipt；Bundle 恢复时重读该证明，不用 Session-local 重算值代替；
- result-bearing Target 的异步 launch request 在建立 TargetRun 前冻结正式 TargetRef、权威且 current 的完整 spec binding／receipt、精确 accepted inputs 与 recoverable requirement；Owner／port admission 在执行副作用前原子验证，TargetRun admission 随后可以先用于实现与审查准备；
- Target、TargetRun 与 Agent Session 是三个身份；一个 TargetRun 根 Agent 只负责一个 Target，但一个 Target 是否可有多个顺序或后继 TargetRun 尚未决定；
- Bundle 可以异步发起 Target 工作并发送 control intent；发起后可以 wait、暂停或结束当前 Session，wait／wake 不传 Target 数据，也不影响 TargetRun 继续执行；
- TargetRun Monitor Loop 持有 raw logs、实时 metrics、snapshot、增量 observation、cursor、停止与恢复；Bundle 不读取这些执行流；
- Target 完成或确需 Bundle 级决策时，以 durable、coalesced、compact TargetWorkNotice 进入 Bundle Inbox；notice 的每个 ref、reason／obligations 集合、单份序列化 envelope、Inbox batch 与完整 handoff 投影都有界，只携带权威 ref、紧凑原因、未决义务与 durable handoff-manifest ref，不替代 Owner receipt；Bundle 级升级的正式 receipt 直接绑定覆盖 blocker、reason、scope 与 obligations 的完整 payload content hash；
- Bundle 消费 terminal handoff 时，权威 frontier 的完整 `current_handle` 与 handoff 最终 handle 精确相等；收口前再次读取的完整 frontier entry 不变；
- 一个精确 Target／TargetRun／Execution Attempt 至多有一份终止型 StopDecision；更换 decision 或 receipt identity 不会产生第二个合法终止事实；
- 复用 source／version 内容绑定实际 exact Implementation Revision；`accepted-local`、`related-history`、`global-baseline-pool` eligibility 另由 eligible TargetCommit 锚定的内容绑定证据和直接绑定其 content hash 的 current receipt 证明，不由 Target 自报；
- seed、replicate、k-fold 与 atomic aggregation 的身份判断只服从 [Bundle 合同的完整测量语义规则](contract.md#测量语义与原子聚合)；Owner seam 必须返回 current accepted aggregation proof，缺失、漂移或无法验证时 fail closed；
- Target Agent 可以提交候选，Research Graph 独占 TargetCommit 接纳；
- Bundle 交接报告是深不可变的 canonical value；所有嵌套容器与 provenance 都不保留可变别名；
- Bundle 根 Session 替换后从权威 frontier、Bundle Inbox、Owner currentness 与已接受 TargetCommit 重建策略；它不依赖父子 transcript；
- Advancement Engine 不逐 Target 签发或推进 frontier。

### 未决合同

```text
TODO-CONTRACT(#63: unresolved Target identity/spec/DAG/frontier,
  TargetRunRequest issuer and StageRun-derived authority,
  Target-to-TargetRun cardinality, TargetRun lifecycle/successor/fencing/
  recovery/cancellation, TargetCommit acceptance/currentness,
  TargetRun-local observation cursor protocol,
  and TargetWorkNotice/Bundle Inbox/handoff manifest production
  issuer/transport/lifecycle)
```

#63 关闭后，把本节同步为已决具名调用和 `TODO-IMPL(...; source=#63)`，再删除本 `TODO-CONTRACT`。

## Harness 映射

优先复用目标 Harness 已有能力：

1. Bundle 根 Agent 用自己的根 Session 规划、读取权威 frontier／Bundle Inbox，并以冻结 exact accepted inputs、current Target spec binding／receipt 与 recoverable requirement 的请求发起异步 Target 工作。Owner／port admission 在创建 TargetRun 或其他执行副作用前原子验证；只向 Bundle 返回 opaque ack。请求返回后可以 wait、暂停或结束；wait／wake 只触发重新读取，不传 Target 数据。
2. TargetRun 使用独立可恢复的根 Session；它不依赖 Bundle Session 存活，并在自己的 Monitor Loop 中持有实时日志、指标、snapshot、cursor、停止与恢复。其内部 spawn／fork 出来的 Agent tree 不逐节点登记到 Runtime 或 Research Graph。
3. TargetRun admission 后，Target Agent 完成一轮整体代码和自检，再形成绑定实际 TargetRun／根 Session、权威 Target spec 与 FormalPlan 的 exact content binding 及各自 exact current acceptance receipt、Implementation Revision 内容接纳 receipt 的 preflight。在该门禁新建专用代码审查子智能体，由它执行 `$code-review` 并返回 findings；review evidence 的不可变 payload 覆盖这些 binding／receipt、fixed base／diff、candidate content、完整 reuse／input／standards scope、reviewer 身份与 review outcome，内容回执直接绑定该 payload hash。一次门禁只审查一个完整 revision，不审查普通编辑中间态；新 revision 使用 fresh review ref、reviewer Session 与 spawn evidence，统一 Session-role 登记拒绝 reviewer 与任一 TargetRun 根 Session 共用 identity。
4. 探索子智能体可以在正式 Target 前运行，但其输出保持 Session-local。
5. Target 根 Session 丢失时，由 Agent Runtime fence 旧资格，再 resume，或按 #63 的权威恢复结果从 TargetRun-local 版本化恢复包建立 replacement Session；Bundle 不读取该恢复包，也不自造当前／后继 TargetRun identity。精确相同的已审查 revision 在纯执行恢复后继续沿用 review evidence。
6. formal submission 只经过相应 Run 的 current 根 Execution Fence；child completion、进程退出或本地文件存在都不能替代 Owner receipt。
7. Target 完成或确需升级时，使 durable、coalesced、compact TargetWorkNotice 可从 Bundle Inbox 读取。Notice 以 durable handoff-manifest ref 暴露最终报告所需的审查、恢复、retired identity 与 receipt refs；正式后继 TargetRun 不删除退役前任。Bundle 级升级的证据冻结 blocker、reason、scope 与 obligations 全部 payload，正式 receipt 直接绑定 payload content hash。新 Bundle Session 只凭权威 Target spec／frontier、Inbox、currentness、handoff manifest 与 Owner refs 重建，不依赖父子 transcript；若 notice 已存在但 frontier 缺失，不得重新派发该 Target。

## 原生结果

Bundle 只在重读权威 frontier／Bundle Inbox／Owner 状态后处理下列结果；wait／wake 本身不携带这些结果。

| Result | 主 Agent 动作 |
| --- | --- |
| `accepted` | 保存精确 ref 与 receipt；更新滚动策略、ExperimentKey coverage 和 ready 工作。 |
| `rejected` | 保存反馈与 receipt；冻结语义未变时保留同一 Target／Stage feedback obligation，并发送修订所需的策略／control intent；TargetRun 根 Session 负责局部修订与重提。 |
| `stale` | 重验 StageRunRequest、Target、输入、frontier generation、binding 与 selected assets；不自动替换成 latest。 |
| `needs_input` | 绑定精确 HumanRequest；只有发起 Owner 保存 satisfied disposition 且该 waiter 通过恢复验证后继续。 |
| `outcome_unknown` | 对账原 operation identity；明确结果前不盲目重放写入。 |
| `technical_blocker` | 保存升级 notice 与权威 blocker refs，按可证明隔离范围调整策略／control intent；TargetRun 根 Session 负责维修与恢复后重验。 |
| `idempotency_conflict` | 停止该写入，保存冲突 evidence，不按到达顺序选择。 |
| `already_accepted` | 验证原 payload／closure 一致后消费既有 ref 与 receipt。 |

RM asset accepted、RG role accepted、Formal Measurement Acceptance、TargetCommit accepted、AR execution completed 与 AE StageCommit 始终分开协调。响应丢失时先查询原 operation identity。

## 监控与终止路由

TargetRun Monitor Loop 完全属于 current TargetRun 根 Session，并可派 monitor 子智能体。它以有界 snapshot／incremental 模式读取 raw logs 与实时 metrics，保存 event cursor／status revision，并校验 MonitorObservation、TechnicalBlocker、SemanticBarrier 与结果 closure 的 current TargetRun／Execution Attempt／Execution Fence binding；恢复后先读新 snapshot，旧执行的迟到 observation 按 retired identity 拒绝。监控轮询只观察，不产生统计停止权。Bundle 主 Session 不接收这些流、snapshot 或 cursor。

Target Agent 可以请求中断，但实际 fence、进程树排空、资源释放与 termination receipt 由 Agent Runtime／受信执行守护完成。同一 Target／TargetRun／Execution Attempt 精确主体至多接纳一份终止型 StopDecision；另一 decision／receipt identity 不能对同一 Attempt 重复终止。TechnicalBlocker 与 recovery 必须返回正式 identity 和 subject-bound receipt；TargetRun 根 Session 对 blocker 的完整身份实行不可变登记，并把每份 recovery receipt 作为只能消费一次的恢复操作。代码修复必须声明 replacement Implementation Revision；replacement Session 先形成绑定新 revision、content hash／acceptance receipt、自检、review scope 与 fresh 独立 reviewer child 的新 preflight，且声明代码变化时不得复用紧邻前一 preflight 的 content hash，TargetRun Monitor Loop 才继续读取受保护 observation；多次代码修复的 preflight 按 recovery transition 顺序保存，最终 implementation provenance 保留初始复用来源和每个实际执行 revision 的内容闭包。未声明 replacement revision 的纯执行恢复只能沿用旧 preflight，拒绝静默 revision 切换或额外 preflight。terminal candidate 的结果 reviewer 由 current TargetRun 根 Session 新建，并与历次代码 reviewer 保持不同 Session／spawn evidence。中断后出现的迟到结果按旧 fence 拒绝，任何已 retired 的 TargetRun／Session／Attempt／Fence 都不能在后续恢复中复活或从证据闭包消失；资源清理失败不复活 Run。

Target 完成或确需 Bundle 级策略、control intent、权限或 HumanRequest 时，形成 durable、coalesced、compact TargetWorkNotice。它以字段级 exact schema 验证的闭合值只携带 subject-bound 权威 ref、单行紧凑原因、无重复未决义务与 durable handoff-manifest ref；每个 ref、集合、单份 notice、Inbox batch 与完整 handoff 投影都有界，不携带或嵌入 raw stdout／stderr、实时 metrics、snapshot、monitor cursor、`MonitorObservation`、完整事件历史、transcript 或隐藏推理。Bundle 级升级证据覆盖 blocker、reason、scope 与 obligations 完整 payload，正式 receipt 直接绑定 payload content hash，旧 receipt 不得用于改写后的 payload。Bundle 可等待 Inbox wake，也可暂停或结束；wake 不携带 payload，同一 generation 下没有新 notice 只是正常的可恢复暂停。Bundle 被唤醒或由新 Session 接手后，在任何 proposal-capable 操作前先读取 durable Inbox；Target 发起前读取权威 Target spec binding、frontier 与 currentness；已有 notice 而 frontier 缺失时先 fail closed，不产生重复派发副作用。消费 handoff 时重新验证 terminal／内容绑定升级／停止到终态关系／按序恢复 review／含退役身份的 evidence 精确闭包；权威 frontier 的完整 `current_handle` 必须与 handoff 最终 handle 精确相等，收口前再次读取并确认完整 frontier entry 未变。SemanticBarrier notice 只提供 subject-bound durable 局部证据；Bundle 按 Target／ExperimentKey 收集并继续协调并行工作，直到全部 remaining key／route 对账且没有 active、blocked、pending 或 unknown 工作，才形成 Bundle 级 `replan_required` 候选。

## Fixture 纪律

参考脚本可以用显式 `fixture-` ref、FakeTargetPort、FakePlanner 和确定性 TargetRun-local observation 演示上述路由。它验证权威 FormalPlan／Target spec 内容绑定与 current receipt、把 Target spec／FormalPlan exact current acceptance receipts 纳入内容绑定的完整 review-evidence scope、Owner seam 对 k-fold 或 multi-seed 返回 [Bundle 合同要求的 current accepted aggregation proof](contract.md#测量语义与原子聚合) 并覆盖合同定义的保留／拆分分支、revision content acceptance、source／version 到实际 Implementation Revision 的内容绑定、TargetCommit 锚定且 receipt 绑定 eligibility content hash 的 Owner 层级资格、subject-wide content hash 不变性、统一 Session-role、Monitor Loop 内受保护 observation 的顺序，以及代码修复后 fresh 独立 review 的正向／拒绝分支；最终 provenance 必须包含实际执行的 replacement revision，纯执行恢复不会重复 preflight 或切换 revision。边界测试还要证明每份 Bundle-facing 根投影，包括 control request／ack，都逐字段拒绝额外字段、自定义 carrier、跨类型 truthy 值与嵌入的 `MonitorObservation`，拒绝非法 UTF-8，并按完整嵌套投影而非单个叶子执行总字节／节点预算；编码与预算错误必须在副作用前 fail closed。它还要验证 launch admission 在创建 TargetRun／frontier／monitor 状态前原子拒绝不精确 inputs、漂移 spec binding／receipt 或缺失 recoverable requirement；Bundle escalation receipt 直接绑定 payload content hash、BundleReport 的嵌套容器深不可变、同一 Execution Attempt 不接受两份终止型 StopDecision，wait／wake 不传数据，Inbox read 先于 proposal-capable 操作，notice 已存在而 frontier 缺失时不会重新派发，terminal handoff 在 Bundle 侧重验停止到终态关系、升级 payload、恢复 preflight 顺序与完整证据闭包，frontier 完整 `current_handle` 与 handoff 最终 handle 精确相等且收口前整份 frontier entry 重确认，并且全新 Bundle Session 可只凭权威 Target spec／frontier、Inbox、currentness、handoff manifest 重建完整报告 refs。多个并行 Target 的 SemanticBarrier 先按 Target／ExperimentKey durable 收集，直到所有 remaining key／route 与外部操作已对账且没有 active、blocked、pending 或 unknown 工作，才允许聚合 `replan_required`。报告保留每次有效 preflight 与退役／后继 TargetRun，而非只保留最终 revision。Fixture 中的 spawn evidence、content hash 与具体 compact bound 是 opaque 测试机制，不证明生产 Harness 已验证真实 parent-child provenance 或内容寻址，也不创建正式 Target、TargetRun、Implementation Revision、EvaluationAttempt、MetricResult、TargetCommit 或 StageCommit。
