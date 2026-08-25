---
name: bundle-stage
description: 从当前 BundleStageRunRequest 与已接受 FormalPlan 滚动规划并异步协调 Target。用于拆分或追加 Target、claim 或恢复 Target 根 Session、处理最终冻结交接、收口 TargetCommit，或提出 Bundle 级阻塞与语义重规划。
---

# Bundle Stage

把 FormalPlan 的证据缺口变成可供 Reasoning 使用的正式测量闭包。Bundle
主 Agent 掌握滚动策略、Target 分解和跨 Target 调度；每个 Target 的根 Agent
Session 独占该 Target 的实现、训练、结果驱动修改和最终冻结交接。State Owner
保留正式身份、内容、测量、接纳与 Stage 推进权。

执行前完整读取 [Bundle 合同](references/contract.md)。首次读取或写入 Owner
状态、claim／wake／cancel Target、恢复 Session 或提交冻结交接前，完整读取
[Owner 操作](references/owner-operations.md)。

## 1. 锁定 Bundle 调用闭包

1. 读取 current BundleStageRunRequest、根 Execution Fence、不可变 ContextPack，
   以及 accepted FormalPlan 的 canonical content hash 和直接绑定该 hash 的 current
   receipt。
2. 完整读取 EvidenceReuseSet、GapSet 和所有 gap ExperimentBrief，核对
   Quest／Cycle／Stage、输入 ref、内容 hash、receipt 与 currentness。
3. 没有 gap ExperimentBrief 时不建立 Bundle Run；把精确 skip 依据交给
   Advancement Engine。

完成标准：所有输入来自同一不可变调用闭包；未接纳内容、本地草稿、漂移引用和
不可验证 receipt 均未进入策略。

## 2. 建立滚动策略

1. 先形成足以启动首批工作的 Session-local 策略，再根据已经接受的
   TargetCommit、最终交接 disposition、资源和 blocker 继续修订；不预先虚构完整
   Target 图。
2. 从每个 ExperimentBrief 的冻结 Goal、Characteristics、BoundaryConstraints、
   SemanticDelta 与 required Metric 归一化 measurement completion cells。
3. 提出最小、可独立收口的 Target 和真实输入依赖。只有下游消费上游新产生且已
   接受的 TargetCommit／asset 时才建立依赖；共享同一已接受输入的工作可以并行。
4. 优先搜索并比较可复用实现，保存 exact source/version/license/patch provenance
   与采用或拒绝理由。探索子智能体只提供 candidate、finding 和 provenance。

完成标准：首批 Target 有明确冻结语义、输入、完成 cell、复用路线与依赖理由；
未规划义务仍显式可见，Session-local 策略没有冒充 RG 的 Target identity、
spec、dependency 或 frontier。

## 3. Claim 独立 Target 根 Session

1. 通过 Owner seam 提交 Target candidate；Research Graph 接受正式 Target
   identity／spec／dependency 后，重新读取 current frontier。
2. 对 ready Target 请求 daemon claim。请求冻结 TargetRef、current spec binding、
   accepted inputs、授权与所需 Harness binding。
3. daemon 只执行 claim、wake、per-Target single-flight、cancel、reconcile 和 event
   forwarding。它为一个 Target 保持至多一个 current 根 Session，不参与实现、
   训练、结果解释、候选选择或 Owner 接纳。
4. Bundle 发起后继续其他工作、wait、暂停或重启均可；Target 根 Session 按 durable
   identity 独立运行。wake 只要求重读权威状态，不携带 Target 结果。

完成标准：每个在途 Target 只有一个 current 根 Session；Bundle 与 daemon 都没有
复制其 workspace、内部循环或结果判断。

## 4. 在根 Session 内完成 Target 循环

1. 根 Session 在专属 Target workspace 中生成、修改或复用实现，并完成与当前
   候选相称的自检。
2. 根 Session 直接使用 Harness 已授权的原生工具启动训练或评估，读取真实结果，
   决定下一轮修改，然后重复“实现 → 自检 → 训练／评估 → 结果驱动修改”。
3. 中间代码、checkpoint、stdout／stderr、日志、分析、临时测量和未采用 Attempt
   全部保持 Target-local。它们可以指导后续工程决策，但不会在循环中成为 RM／RG
   接受事实。
4. 根 Session 可以派探索、代码审阅或结果审阅子智能体；子智能体只返回
   candidate／finding／provenance。根 Session 逐条处置 finding，并独占 workspace
   修改、训练启动、checkpoint 选择和最终候选选择。
   对长训练或长日志，根 Session 也可以派一个聚焦子智能体持续观察进程、tail 日志
   并汇报进度；该子智能体不替根 Session 修改代码、停止训练或决定最终交接。
5. 所有迭代保持 FormalPlan 冻结语义。确需改变 Goal、Characteristics、
   BoundaryConstraints、SemanticDelta、required Metric 或 held-fixed 条件时，
   形成类型化 Bundle 升级，不把变化伪装成局部修复。

单轮完成标准：一次真实训练／评估已有可追溯输入和输出，根 Session 已明确继续
修改、保留候选或升级。局部循环完成标准：已选择 terminal candidate，所有在途
进程与失联副作用均已 reconcile，且 workspace 不再发生会改变交接内容的写入。

## 5. 冻结最终交接

1. 只有代码实现和最后一次训练／评估都真正完成、在途进程已经对账后，根 Session
   才返回一个闭合 `TargetCompletionHandoff`。它只声明 TargetRef、TargetRunRef、
   `completed`、所选 artifact 的相对路径与 role、唯一 result document 路径和摘要。
2. 根 Session 不创建 hash、receipt、VariantRun、EvaluationAttempt、MetricResult 或
   TargetCommit，也不需要理解 Owner 的接纳流程。Owner 从 workspace 重新读取并计算
   bytes、tree、size 与 manifest；不能信任 Agent 自报这些事实。
3. daemon 只转发 handoff-ready 事件并重放 finalizer。lost ACK 或 restart 必须查询并
   重用同一 Harness evidence，不能再次运行根循环或重新解释 stdout。

完成标准：handoff 是一个小而闭合的路径选择；没有 active process、待定输出、可变
路径或 live stdout 被当成交接事实。

## 6. 在冻结后请求 Owner 接纳

1. 只有 TargetCompletionHandoff 存在后，Research Memory 才接受其中明确选中的
   implementation、checkpoint、result、log 与 analysis bytes。
2. RM receipt 齐备后，Research Graph 才根据 accepted Target spec、FormalPlan、
   冻结输入和 RM result document 派生并接受正式测量与 TargetCommit。根 Session
   不提交这些正式身份；Agent Runtime 只证明 root evidence 与 lineage。
3. stdout／stderr 与 Web event projection 不参与 Metric 计算或接纳。正式 Metric
   只来自 handoff 选定的 result document 与已有冻结测量合同。
4. Owner 拒绝不会改写原 handoff，也不能以 Web stdout 或本地文件绕过验证。

完成标准：Target-local execution、completion handoff、RM asset acceptance、RG formal
measurement、TargetCommit 与 StageCommit 是可分别查询的事实；没有任何生成内容
在最终冻结交接前进入 RM／RG。

## 7. 收口 Bundle

1. 重读权威 frontier 和 Owner receipts，逐 ExperimentKey 收集 accepted
   TargetCommit、remaining route、technical blocker 或 SemanticBarrier。
2. 保留已接受负、零、不显著和不确定结果；它们是 realized 测量，由 Reasoning
   解释。技术故障、pending、unknown outcome 或仍可执行路线保持在途。
3. 所有 completion cells 都由 current accepted TargetCommit 覆盖时，形成 deeply
   immutable BundleReport 候选；需要改变冻结语义时形成 replan_required 候选；
   冻结合同内没有路线且所有副作用均已对账时才提出 ExhaustionProposal。
4. 把候选交给获授权 Owner。只有 Advancement Engine 可以验证 current request、
   BundleReport 与全部 receipts 并形成 StageCommit。

完成标准：每个 gap、Target、Session、最终 handoff 和外部副作用都有明确去向；
Bundle 没有把实时事件、workspace 状态、执行完成、资产接纳、测量接纳、
TargetCommit 或 Stage 推进合并成一个事实。
