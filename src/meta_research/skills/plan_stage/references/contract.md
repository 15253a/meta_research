# Plan 语义契约

## 调用闭包

`PlanStageRunRequest` 冻结 request、Cycle、epoch、Execution Fence、运行绑定、ContextPack ref／hash、已接纳 Question 与 IdeaSet 的绑定和正文，以及证据引用版本。Plan 消费完整 `IdeaSet`；`NoViableCandidate` 交给 Reasoning，不能生成空 Plan。

Question 绑定含精确 Question／Quest／内容 ref、hash／schema 与 RM／RG receipts。IdeaSet 绑定还含 Idea StageCommit 及完整正文。绑定缺失、receipt 不可验证或 hash 不符时保留技术阻塞。

## AnswerContract

obligations 是本轮选择承担的调查责任和复盘条件，不是保证研究成功。后继 Plan 根据新证据与上轮综合重新判断；旧 Plan 保持当时不可变语义，其覆盖和 Brief 不随引用自动更新。局部开工条件仅约束真正依赖它的工作。

```text
AnswerContract
  source_question_ref
  source_idea_set_ref
  obligations[1..N]
    obligation_key
    statement
    minimum_support
    question_trace[1..3] = unknown_statement | answer_shape | applicability_scope
    idea_relevance[实际有关的候选]
      idea_ref
      role = query_lens | experiment_lens | not_relevant
      rationale
  answer_contract_hash
```

每项义务追溯到 Question；`idea_ref` 逐字使用已接纳 IdeaSet 的 `candidate_key`，每个引用唯一并说明影响，无关候选无需逐项列出。Idea 可细化证据、比较、条件或证伪边界，义务仍保持当前 Question 范围。`answer_contract_hash` 在使用 Evidence 前冻结。

## 证据与覆盖

Target 来源的 `EvidenceRef` 绑定精确 AssetVersion、内容／manifest hash、TargetCommit、真实生产者及 RM／RG 接纳事实；有测量时保留实际评价来源，无评价工作保持 WorkProduct 来源，不补造测量。HumanInput、ScientificOutcome、AssetVersion、LiteratureSnapshot 采用各自精确来源绑定，详见[Owner 操作](owner-operations.md)。目录卡片、摘要、排序、动态 URL 和本地路径用于导航，不代替原文或接纳链。

```text
EvidenceUse
  obligation_key
  evidence_ref
  supported_claim
  support_boundary
  contributing_idea_refs[]

CoverageDecision
  obligation_key
  disposition = covered | gap
  evidence_uses[]
  insufficiency | null
```

每项义务恰出现一次。`covered` 至少有一个实际 EvidenceUse，`insufficiency=null`，没有对应 Brief。`gap` 以非空 insufficiency 说明不足，可保留部分 evidence use；未安排时同时说明本轮不安排的理由、依据与限制。明确检索范围内证据不足可支持 gap；空分页或未展示条目不证明全库不存在。来源不可用、receipt／hash 冲突和未知结果先作为技术问题处理。

## Brief 与后继

每个 Brief 有 Plan 内唯一 `experiment_key`、非空且本 Brief 内不重复的合法 gap obligation keys、goal、characteristics、boundary constraints、semantic delta 和实际 Idea 来源。可只推进部分 gap，多份 Brief 也可推进同一 gap。可选 `reuse_material_refs[]` 用 evidence_ref 引用统一采用集合中的计划复用材料，省略或为空均合法；Bundle 再按实际用途选择实施输入。Plan 不形成 Target 身份、执行 DAG、Worker 或调度。

Brief 为空 → `no_new_experiment_required`；有 Brief → `experiments_required`。前者由 AE 按已接纳 Plan 形成 `StageCommit(Skipped)`，Reasoning 仍消费完整 Plan、采用来源、真实缺口及未安排理由。Plan 接纳与 Bundle skip 分别表达规划与路由，科研判断由 Reasoning 综合。

`source_bindings.selected_evidence_bindings` 原样保存 Plan 提交的完整采用集合，包括只被 Brief 引用的材料。`evidence_reuse_set` 只从各 CoverageDecision 的 EvidenceUse 派生，不能重建或裁剪该集合。后续 Target 的新交付沿正式接纳、Bundle 整理与 Reasoning 综合进入研究积累，已接纳 Plan 保持当时判断。

## 接纳交接

独立审阅按[主 Skill](../SKILL.md)执行。RM 保存不可变 PlanDocument，RG 接纳为 FormalPlan 或返回结构反馈。RM 已接纳而 RG 拒绝时保留该内容，在同一根 Session 修订成新内容身份。RM／RG／AR receipts 与当前 AE request／epoch 均可验证后，由 AE 提交 StageCommit。
