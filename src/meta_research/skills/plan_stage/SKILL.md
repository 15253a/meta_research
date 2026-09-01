---
name: plan-stage
description: 从当前 accepted Question binding 与完整 accepted IdeaSet 形成并评审可审计 PlanDocument 候选。用于 Plan Stage 推导 AnswerContract、核对 EvidenceRef、判定 coverage/gap、仅为 gap 形成 ExperimentBrief，并把候选交给 RM/RG Owner 接纳。
---

# Plan Stage

把一个冻结的 Plan invocation closure 收口成 `PlanDocument` 候选。拥有 AnswerContract、证据充分性、gap、ExperimentBrief 与 advisory review disposition 的研究判断；把内容保管、FormalPlan 身份、Run 与 Stage 推进留给对应 State Owner。

开始前完整读取[语义合同](references/contract.md)和[Owner 操作](references/owner-operations.md)。

## 1. 锁定闭包

1. 核对当前 `PlanStageRunRequest`、Execution Fence、runtime binding、ContextPack ref/hash、accepted Question binding/content 与 accepted IdeaSet binding/content。
2. 只消费同一 Quest、Cycle、request 与 epoch 下的精确 ref、hash、schema 和 RM/RG/AE receipt；把缺失、漂移、不可用或未知结果返回为 typed blocker。
3. 把 ContextPack 的 Evidence catalog 当成被冻结的可引用候选集合；搜索发现若没有当前 Owner 验证的精确 `EvidenceRef`，只作为研究观察。

完成标准：Question、完整 IdeaSet 与 Evidence catalog 属于同一不可变调用闭包。

## 2. 冻结 AnswerContract

1. 从 Question 的 unknown、answer shape 与 applicability scope 推导非空 obligations。
2. 让每个 obligation 追溯到 `answer_shape` 和至少一个其他 Question 字段。
3. 对每个 `obligation × IdeaCandidate` 恰好记录一个 `query_lens | experiment_lens | not_relevant` 角色及理由。
4. 在使用 EvidenceRef 前冻结 `answer_contract_hash`；语义变化产生新的合同闭包。

完成标准：每个 obligation 和每个 IdeaCandidate 都被完整交代，Question 语义没有扩张。

## 3. 编译 PlanDocument

1. 对每个 obligation 恰好形成 `covered | gap` 判定。
2. 只用 ContextPack 中精确、不可变、当前可验证的 EvidenceRef；逐项记录 supported claim 与 support boundary。
3. `covered` 至少有一个 evidence use 且没有 ExperimentBrief；`gap` 记录 insufficiency 并至少由一个 Brief 收口。
4. 让 Brief 覆盖全部且只覆盖 gap。保存 goal、characteristics、boundary constraints、semantic delta 和 Idea provenance；把 Target、DAG、实现路线、Worker、Provider 与资源调度留给 Bundle。
5. 机械派生 disposition：无 gap 且无 Brief 为 `no_new_experiment_required`；存在 gap 且全部由 Brief 收口为 `experiments_required`。

完成标准：AnswerContract、EvidenceReuseSet、coverage、GapSet、Brief 与 IdeaTrace 彼此闭合。

## 4. 根会合质询

1. Owner 保存 primary draft checkpoint 后，在同一 managed native 根 Session 发起第二个 provider turn，只处理冻结闭包和完整草稿。
2. 根 Agent 重新检查 Question 对齐、obligation/Idea 完整性、Evidence 支持边界、gap/Brief 闭合和 Owner 权限边界，形成 bounded findings；它不批准内容。
3. 对每条 finding 给出唯一 `revised | not_adopted` disposition，并返回最终完整 PlanDocument。`revised` 必须产生实质变化。当前 record 固定为 `advisory_unobserved`、null reviewer、`independent=false`。

完成标准：真实第二个 provider turn 已完成并绑定 response hash，且每条 finding 均有处置。

## 5. 提交与恢复

1. 正式写入前重验调用闭包与全部入选 EvidenceRef。
2. 先让 Research Memory 接受不可变 PlanDocument 内容，再让 Research Graph 接受精确内容绑定及 FormalPlan 语义。
3. 保留 RM checkpoint 与 RG decision 的独立 identity/receipt。`rejected` 在同一根 Session 中按正式 feedback 实质修订；未知结果只协调原 operation identity。
4. RG accepted 后才允许 Agent Runtime 形成 execution-completed receipt；Advancement Engine 只在当前 request/epoch、AR receipt 与全部 Owner receipt 完整时形成 Plan StageCommit。
5. `no_new_experiment_required` 只形成可验证的 Bundle skip basis；不伪造 Bundle Run。

完成标准：每个外部效果有类型化结果，恢复从第一个缺失的 durable receipt 继续且不重复副作用。

## 收口检查

- 永久保持 `execution completed != content accepted != domain accepted != Stage advanced`。
- 只把 RG 接受且精确绑定 RM 内容的对象称为 FormalPlan。
- Skill 不创建 Owner receipt、StageCommit、Bundle Run、Target、DAG、Worker 或 Provider 身份，也不接纳自己的候选。
