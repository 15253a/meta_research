# Bundle Stage 合同

本文件是 Bundle 滚动策略、Target 粒度、Target 根 Session 循环、最终冻结交接、
测量闭包与 Bundle disposition 的唯一语义来源。

## 目录

- [调用闭包](#调用闭包)
- [滚动策略与 Target](#滚动策略与-target)
- [测量身份与粒度](#测量身份与粒度)
- [依赖与复用](#依赖与复用)
- [Target 根 Session](#target-根-session)
- [Target daemon](#target-daemon)
- [结果驱动循环](#结果驱动循环)
- [恢复与取消](#恢复与取消)
- [TargetCompletionHandoff](#targetcompletionhandoff)
- [交接后的 Owner 接纳](#交接后的-owner-接纳)
- [Web 执行观察](#web-执行观察)
- [Bundle disposition 与 Stage 交接](#bundle-disposition-与-stage-交接)

## 调用闭包

BundleStageRunRequest 冻结同一份：

- stage request、Cycle、epoch、Bundle Run 与根 Execution Fence；
- ContextPack ref 与 canonical content hash；
- accepted FormalPlan ref、canonical content hash，以及直接绑定该 hash 的 current
  acceptance receipt；
- EvidenceReuseSet、GapSet 与全部 gap ExperimentBrief；
- Quest authorization、Harness binding 与资源边界。

Bundle 只消费 Research Graph 已接受且 current 的 FormalPlan。稳定 ref 不能替代
内容绑定；同一 FormalPlanRef 下任何 ExperimentBrief、冻结语义、完成 cell 或输入
引用变化后，旧 hash 与 receipt 均失效。本地 PlanDraft、未接受内容和漂移的 latest
引用不能启动 Target。

没有 gap ExperimentBrief 时不建立 Bundle Run 或 Target。Advancement Engine 根据
current FormalPlan 和 Owner receipts 形成显式 StageCommit(Skipped)。

## 滚动策略与 Target

Bundle 主 Agent 在首次 claim 前读取整个调用闭包，并维护一份可修改、可丢弃的
Session-local RollingBundleStrategy。它至少追踪：

- unresolved ExperimentKeys 与 measurement completion cells；
- accepted upstream closures；
- local Target candidates 与真实输入依赖；
- reuse／implementation choices；
- ready-work、资源与并行假设；
- accepted-result coverage、typed blockers 与 semantic risks。

该策略不是正式 Target、dependency graph、frontier、TargetRun、receipt 或跨 Stage
artifact。主 Agent 可以先形成足以启动首批工作的局部策略，再根据 accepted
TargetCommit、冻结交接 disposition、资源与反馈提出后续 Target；它不必一开始列出
全部未来路线。

Research Graph 独占正式 Target identity、canonical spec、dependency 与 current
frontier。一个 Target candidate 获得 TargetRef 时，RG 的 current spec binding 必须
冻结其 ExperimentKey、目标、输入、完成 cell、复用 trace、冻结语义与依赖。Bundle
恢复时重读该 binding 和 receipt，不用 Session-local 重算值代替。

策略修改只改变尚未提交的候选与内部调度。需要改变 FormalPlan 的 Goal、
Characteristics、BoundaryConstraints、SemanticDelta、required Metric 或 held-fixed
条件时，形成 replan_required 候选。

## 测量身份与粒度

一个 result-bearing Target 以形成一个可接纳的独立测量闭包为目标。最终
TargetCommit 恰好选择一个获得 Formal Measurement Acceptance 的 EvaluationAttempt
和该 Attempt 唯一的 MetricResult。

领域身份按被接纳的因果语义划分，而不是按文件、Agent、进程、训练命令、seed、
fold、checkpoint 数量或物理并发划分：

- Baseline 冻结模型主体的 forward 语义；
- Variant 冻结训练、状态形成与模型侧推理配方；
- VariantRun 表示一份精确输入绑定下的实际状态形成；
- ProtocolVersion 冻结评估数据、评估预处理、required Metric、停止与聚合语义；
- Evaluation 是精确 Variant × ProtocolVersion；
- EvaluationAttempt 是该 Evaluation 的一次实际测量，精确绑定所用 VariantRun 与
  CheckpointArtifact；
- MetricResult 只属于一个获得正式接纳的 EvaluationAttempt。

技术 retry、Session resume、子智能体故障或同一语义内的代码调试不会自动建立新
VariantRun／EvaluationAttempt。一次语义独立的状态形成才建立新 VariantRun；一次
语义独立的实际测量才建立新 EvaluationAttempt。

seed、replicate、k-fold 等多个 part 只有在 FormalPlan 把它们定义为同一原子接纳
单元、ProtocolVersion 冻结完整有序 part set 与 aggregation rule、且任一 part
不会自适应改变后续训练或选择时，才可属于同一 EvaluationAttempt。被独立消费、
解释、选择或用于改变后续状态的 part 必须按其真实因果身份拆分。

## 依赖与复用

依赖只表达实际输入消费。只有下游 Target 需要上游新产生且已接受的 TargetCommit
或 asset 时才建立依赖；共享同一已接受锚点、互不消费对方新结果的 Target 可以
并行。用于自适应选择下游路线的 accepted result 也是实际依赖。

Bundle 从局部向外搜索实现：

1. 当前 Target 的 accepted upstream closure 与本轮共享实现；
2. 当前 FormalPlan／Question 的已接受历史；
3. Baseline Pool 中 current、eligible、可验证的 TargetCommit closure；
4. 固定版本的成熟外部包、论文实现或源码；
5. 有明确理由的自行实现。

每个正式候选保存 exact source/version、实际选中内容 hash、license／NOTICE、
patch provenance、采用或拒绝理由。accepted-local、related-history 和
global-baseline-pool 等资格必须锚定 eligible TargetCommit，不能由 Target 自报。

当实现不是计划内变量时，所有比较单元绑定同一份 held-fixed exact implementation
content。代码修改产生新内容身份；它可以在 Target-local 循环中反复出现，但最终
所选结果必须绑定实际生成该结果的 exact implementation。若修复必须改变冻结
held-fixed 条件，则升级为 replan_required。

## Target 根 Session

保持三个身份：

- Target：Research Graph 的持久研究工作身份；
- TargetRun：Agent Runtime 为一个 Target 管理的可恢复执行 envelope；
- Target root Session：Harness 中唯一 current、可持续工作的根 Agent Session。

每个 claimed Target 同时至多有一个 current 根 Session。它独占：

- Target workspace 的写入；
- 实现或复用选择与代码修改；
- 自检、训练和评估命令；
- 对中间结果的解释与下一轮修改；
- checkpoint 与 terminal candidate 的选择；
- 最终闭合 TargetCompletionHandoff 的选择与返回。

Bundle 主 Session、daemon 和子智能体都不复制这条决策循环。根 Session 可以用
Harness 原生 spawn／fork 创建聚焦的探索或审阅子智能体；子智能体只返回
candidate、finding 与 provenance，不取得 Target 身份、workspace 最终写权、
训练启动权、候选选择权或 Owner 接纳权。根 Session 对每条 finding 作出 disposition。
长训练或长日志可以由一个聚焦子智能体持续观察进程、tail 输出并向根 Session 汇报；
它不修改代码、不决定停止、checkpoint 或完成，且不是 daemon-owned Supervisor。

Target 根 Session 直接使用 Harness 按 Quest authorization 提供的原生 shell、file、
stream 与 native Session；MCP、Skill 和子智能体是可用时才选择的辅助能力，不要求
每轮实际调用。真正需要的能力缺失时才形成 typed blocker。

## Target daemon

Target daemon 是机械协调层，职责完整限定为：

- claim：把一个 current、ready Target 和冻结输入绑定到根 Session；
- wake：向已 claim 的根 Session 或 Bundle 传递“重新读取权威状态”的 durable 信号；
- single-flight：保证一个 Target 至多有一个 current 根 Session；
- cancel：投递已授权取消并阻止 cancelled identity 继续产生 current effect；
- reconcile：在 lost ACK、daemon restart 或 Session reconnect 后恢复同一 claim、
  Session 与 TargetCompletionHandoff evidence；
- event forwarding：把根 Session 的 stdout、stderr 与工具生命周期事件转发到
  已认证 Web。

daemon 不实现代码、不启动独立训练策略、不解释结果、不选择 checkpoint、不生成
Metric、不决定何时冻结，也不替 RM／RG／AR／AE 接纳领域事实。它可以验证 claim、
currentness、authorization、single-flight、event identity 与幂等键，但这些机械
检查不能变成科研决策。

claim 响应只证明一个根 Session 已获得 current 工作资格。Bundle 发起后可以继续
其他工作、wait、暂停或重启；Target 根 Session 独立运行。wake 不携带结果、日志、
Metric 或 Owner receipt，接收者总是重新读取权威状态。

## 结果驱动循环

根 Session 重复以下闭环，直到形成 terminal candidate、需要 Bundle 级语义决定、
收到取消，或出现无法在当前授权内恢复的 blocker：

1. 生成、修改或复用完整实现；
2. 对当前候选执行必要自检；
3. 用 Harness 原生工具训练或评估；
4. 读取 stdout、日志、checkpoint、结果文件和分析；
5. 根据结果修改实现、训练配方或 Target-local 路线，然后重试；
6. 在满足冻结语义与完成合同后选择 terminal candidate。

中间 implementation revision、checkpoint、日志、分析、临时读数与失败路线都保持
Target-local。它们进入可恢复 workspace，但在循环中不进入 Research Memory 或
Research Graph 接纳链。

中间结果可以驱动工程迭代。若某个测量被用于自适应选择训练、checkpoint、参数、
停止或路线，根 Session 必须在最终 result document 中如实说明，不能把同一数据
冒充未触碰的最终评估。正式 EvaluationAttempt 与 ProtocolVersion 由 Research
Graph 在交接后依据已接受合同派生，不由根 Session 自行创建。

准备冻结前，根 Session 对完整 terminal candidate 做最终自检，并可新建 fresh
代码或结果审阅子智能体。审阅只产生 findings；任何修改都回到根循环并重新生成
与实际结果匹配的 terminal candidate。普通编辑中间态不形成正式审阅或 Owner
接纳记录。

## 恢复与取消

daemon reconcile 优先恢复同一 claim、根 Session 和 workspace。恢复后的根 Session
先对账在途命令、未确认工具效果和最后一次 durable checkpoint，再决定继续；它
不会根据 Web event 缺口猜测外部效果。

同一 Target 的并发 claim fail closed。无法恢复原生 Session 时，Agent Runtime
保留 retired identity 和已知副作用，并按权威恢复结果建立一个明确后继根 Session；
single-flight 在后继启动前使旧 identity 永久失去 current 资格。后继从同一
Target-local durable workspace 与审计 manifest 恢复，不从 Bundle transcript 猜测。

cancel 只由已授权请求触发。daemon 投递 cancel、等待 Harness 确认并保存机械结果；
根 Session 对账已启动命令和文件后终止。cancel 前已经完整冻结且 identity 可验证的
handoff 保持独立候选；live workspace、迟到 stdout 和未冻结结果不能越过取消边界。

## TargetCompletionHandoff

根 Session 只有在下列条件全部满足后才能冻结：

- terminal candidate 已选定，并绑定实际生成其结果的 exact implementation；
- 所有在途命令、失联副作用与输出写入都已 reconcile；
- Frozen FormalPlan 语义、accepted inputs 与 current Target spec 仍可验证；
- final self-check 和所有采用的 review findings 已处置；
- 选中的 implementation、checkpoint、result、log、analysis 路径已经稳定；
- 没有 live path、可变容器、进程句柄或 stdout 流被当成正式内容。

TargetCompletionHandoff 是一个小而闭合的 canonical value，字段只有：

- schema_ref、TargetRef、TargetRunRef 与固定 `completed`；
- 一到多项 `{role, relative_path}`，role 仅限 implementation、checkpoint、result、
  log、analysis；
- 唯一 `result_document_path`，且必须指向一项 result artifact；
- bounded summary。

根 Session 不自报 content hash、receipt、正式测量身份或 TargetCommit。Harness 只从
current root 的最终 agent message 提取该值；Owner 再扫描 workspace、重算 bytes 与
manifest 并冻结。lost ACK 或 daemon restart 必须查询并重用同一 Harness evidence。

## 交接后的 Owner 接纳

Research Graph 可以在执行前接受 Target identity／spec／dependency；本节的
“交接后接纳”专指 Target 根循环新产生的 implementation、checkpoint、result、
measurement 与 commit facts。

顺序保持为：

1. Agent Runtime 验证 handoff 来自 current TargetRun／root Session 的 terminal
   Harness evidence；
2. Research Memory 扫描并接受 handoff 明确选择的 implementation、
   checkpoint、result、log 与 analysis bytes，并为每份 immutable content 签发
   receipt；
3. Research Graph 使用这些 RM receipts、accepted Target spec、FormalPlan 与冻结
   输入派生并接受相应角色、VariantRun、EvaluationAttempt、Formal Measurement 与
   唯一 MetricResult；
4. Research Graph 在完整测量闭包 current 时接受 TargetCommit；
5. Bundle 只在重读 TargetCommit 与 receipts 后更新 coverage。

永久保持：

root turn completed
!= TargetCompletionHandoff verified
!= RM assets accepted
!= RG formal measurement accepted
!= TargetCommit accepted
!= Stage advanced

Owner rejection 不修改原 handoff，也不能通过本地文件或 Web event 绕过。语义合同
本身需要改变时升级 Bundle，不在已完成 handoff 内静默修改。

## Web 执行观察

daemon 可以把带 Target／TargetRun／root Session identity 的 stdout、stderr 与工具
事件转发到 authenticated Web。Web 展示 bounded live tail、历史／current 标签和
连接状态；断线后可以重新订阅，但 event forwarding 不保证科研事实的完整性。

这些事件是 observation，不是 Metric authority：

- Web 不从 stdout 文本、进度条、日志标签或临时 JSON 生成 MetricResult；
- stdout 中出现 accuracy、loss、p-value 或 completed 不产生 Owner receipt；
- live event 的丢失、重复、迟到或重连不改变 Target root Session 的决策状态；
- 正式 Metric 只来自 TargetCompletionHandoff 选中的 RM-accepted result document，
  经已接受测量合同验证后由 Research Graph 接受。

## Bundle disposition 与 Stage 交接

合法结果保持：

| disposition | 充分条件 |
| --- | --- |
| realized | ExperimentBrief 所需 cells 都有 current accepted TargetCommit；结果方向不限。 |
| blocked | 技术、权限、资源、currentness 或 unknown outcome 阻止可信继续。 |
| replan_required | 剩余有效路线必然改变冻结 FormalPlan 语义。 |
| ExhaustionProposal | 冻结合同内无实质不同路线，且所有 Target、handoff、取消、HumanRequest 与 unknown effect 均已对账。 |

负、零、不显著、否定或不确定的正式测量仍是 realized，由 Reasoning 解释。技术
失败、缺 Metric、pending handoff、Owner rejection、active Target 或 unknown
outcome 不得伪装为 replan_required 或 exhaustion。

Bundle 输出 deeply immutable BundleReport 候选，包含 exact StageRunRequest、
FormalPlan、逐 ExperimentKey coverage、accepted TargetCommits、每个 accepted
TargetCompletionHandoff、RM／RG／AR receipts、remaining work、blocker／replan／
exhaustion evidence。它不包含 live workspace、stdout stream、未冻结 Metric 或
子智能体 transcript。

只有 Advancement Engine 可以在 current StageRunRequest、BundleReport 和全部
Owner receipts 均可验证后形成 StageCommit。Bundle root Session、Target root
Session、daemon、Web event 和本地文件均不能推进 Stage。
