---
name: plan-stage
description: 形成与恢复 FormalPlan。用于 Plan Stage 从当前 PlanStageRunRequest 编排历史证据、收口证据缺口、产出 ExperimentBrief 或判断 Bundle 是否可跳过。
---

# Plan Stage

把一个已接受的 Question 和完整 IdeaSet 收口成一个可审计的 FormalPlan。由当前根 Session 内的主 Agent 负责研究语义；由各 State Owner 负责内容保管、正式身份、回执和 Stage 推进。

执行前，完整读取 [Plan 合同](references/contract.md)；它是输入、AnswerContract、EvidenceRef、覆盖、ExperimentBrief、FormalPlan、审阅、跳过与耗尽语义的唯一来源。

首次查询证据或调用 Owner 操作前，读取 [Owner 操作](references/owner-operations.md)；它定义权限、结果恢复和待实现端口。

主 Agent 可以按任务需要调用一个或多个子智能体，扩展检索、核验、审阅和并行分析能力。按 Owner 操作约束其输入与权限，并由主 Agent 汇总建议、作出语义判断和发起正式提交。

## 1. 锁定调用闭包

1. 取得类型化且当前有效的 `PlanStageRunRequest`、根 Execution Fence、不可变 ContextPack，以及精确绑定的已接受 Question 和完整 IdeaSet。
2. 按 Plan 合同核对身份、内容哈希、schema、Owner 回执、Question／Quest／Cycle 绑定、搜索边界与当前性。
3. 把缺失或未知的身份、当前性、可用性、完整性、回执、端口或冻结输入成员关系路由为类型化技术阻塞。

完成标准：所有输入属于同一个不可变调用闭包，且每项上游事实均有可验证来源。

## 2. 冻结候选 AnswerContract

1. 从 Question 的 unknown、answer shape 和 applicability scope 推导本 Session 的 Evidence obligations。
2. 对每个 `obligation × IdeaCandidate` 逐一判定 `query_lens | experiment_lens | not_relevant`，并记录理由。
3. 让相关 Idea 收紧证据要求、对照、条件、干预轴或证伪边界，同时保持 Question 语义不变。
4. 在查询证据前冻结 `answer_contract_hash`；语义变化时开启新的查询闭包。

完成标准：每个 obligation 可追溯到 Question，每个 Idea 均被逐项交代，候选合同没有隐式语义扩张。

## 3. 查询并判断精确证据

1. 在稳定搜索快照中按 obligation 查询，并沿不透明 `next_hop` 到达精确叶子。
2. 只选择符合 Plan 合同的 `EvidenceRef`，为每次使用记录 supported claim 与 support boundary。
3. 由主 Agent 判断科学相关性与充分性；把 Owner eligibility 视为可引用资格，而非充分性结论。
4. 对过期快照或失效证据执行显式刷新、重新查询和重新验证。

完成标准：每个入选 EvidenceRef 均来自同一 AnswerContract 闭包、当前可验证，并具有明确支持边界。

## 4. 编译一致的 PlanDraft

1. 按 Plan 合同把每个 obligation 恰好归为 `covered` 或 `gap`。
2. 从覆盖判定派生 `EvidenceReuseSet`、`GapSet`、ExperimentBrief 列表和 Bundle disposition。
3. 让每个 gap 至少由一个 ExperimentBrief 收口，并让每个 ExperimentBrief 只服务已声明的 gap。
4. 把 Target 分解、实现路线、资源和调度交给 Bundle。

完成标准：覆盖判定完整且无冲突；gap 与 ExperimentBrief 双向闭合；Bundle disposition 可由内容机械派生。

## 5. 完成一次独立质询

1. 在首次正式提交前，把完整草案和冻结绑定交给一个新的独立审阅者。
2. 按 Plan 合同限制审阅范围，并把每条 finding 归为 `revised | not_adopted`。
3. 由主 Agent 完成至多一次修订，并记录草案与最终哈希。

完成标准：每条 finding 都有处置，任何修订都能由哈希证明，审阅结果保持 advisory。

## 6. 提交并恢复

1. 写入前重新验证调用闭包和所有入选证据。
2. 先向 Research Memory 提交不可变 Plan 内容，再把精确内容绑定与 Plan 语义提交给 Research Graph。
3. 按 Owner 操作处理 `accepted | rejected | stale | needs_input | outcome_unknown | technical_blocker`，并保留每次外部效果的身份与回执。
4. 在当前根 Session 内修订或恢复；对未知结果先协调原操作身份。

完成标准：每个外部效果都有类型化 Owner 状态；只有 Research Graph 接受的精确内容绑定被称为 FormalPlan。

## 7. 交接或提出耗尽

1. 对已接受的 FormalPlan，返回 Plan ref、AnswerContract hash、内容 ref、RM／RG 回执、证据与 gap 闭包，以及派生的 Bundle disposition。
2. 对 `no_new_experiment_required`，输出 `BundleSkipBasisCandidate`，交由 Advancement Engine 验证并形成 `StageCommit(Skipped)`。
3. 仅在 Plan 合同的严格条件全部满足时提出 ExhaustionProposal。

完成标准：主 Agent 能逐项说明每个 obligation、Idea、EvidenceRef、gap 和 Owner 结果的去向，且正式权限均由对应 Owner 行使。

## 不变量

```text
execution completed != content accepted != FormalPlan accepted != Stage advanced
```

- 依据 support boundary 判断负结果、零结果、不显著结果或不确定结果是否覆盖 obligation。
- 把 Card、投影、审阅意见、本地文件和 fixture 输出保留为导航或建议材料。
- 把 Question、Idea、Target、Run、StageCommit 和 Owner 回执交给各自权威来源。

运行确定性参考测试：

```bash
python -B scripts/test_plan_stage_mvp.py
```
