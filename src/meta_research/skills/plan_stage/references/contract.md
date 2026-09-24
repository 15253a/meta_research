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

每项义务恰出现一次。`covered` 至少有一个实际 EvidenceUse，`insufficiency=null`，没有对应 Brief。`gap` 说明不足，可保留部分 evidence use，至少由一个 Brief 覆盖。明确检索范围内证据不足可支持 gap；空分页或未展示条目不证明全库不存在。来源不可用、receipt／hash 冲突和未知结果先作为技术问题处理。

## Brief 与后继

每个 Brief 有 Plan 内唯一 `experiment_key`、一个或多个 gap obligation keys、goal、characteristics、boundary constraints、semantic delta 和实际 Idea 来源。全部且只有 gap 需被 Brief 覆盖。Plan 不形成 Target 身份、执行 DAG、Worker 或调度。

全部 covered 且无 Brief → `no_new_experiment_required`；存在 gap 且均有 Brief → `experiments_required`。前者只给 Bundle skip 候选，由 AE 核验后形成 `StageCommit(Skipped)`。

## 接纳交接

独立审阅按[主 Skill](../SKILL.md)执行。RM 保存不可变 PlanDocument，RG 接纳为 FormalPlan 或返回结构反馈。RM 已接纳而 RG 拒绝时保留该内容，在同一根 Session 修订成新内容身份。RM／RG／AR receipts 与当前 AE request／epoch 均可验证后，由 AE 提交 StageCommit。
