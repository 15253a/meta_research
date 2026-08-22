# Plan Stage 语义合同

## Invocation closure

`PlanStageRunRequest` 冻结 request、cycle、epoch、Execution Fence、runtime binding、ContextPack ref/hash、accepted Question binding/content、accepted IdeaSet binding/content 和 Evidence reference revision。Plan 只消费完整的 accepted `IdeaSet`；accepted `NoViableCandidate` 是交给 Reasoning 的负向结果，不能制造空 Plan。

`AcceptedQuestionBinding` 包含精确 Question/Quest/content ref、content hash/schema 与 RM/RG receipts。`AcceptedIdeaSetBinding` 包含精确 IdeaSet ref、content ref/hash、RM/RG receipts、Idea StageCommit ref/receipt 与完整 IdeaSet data。任一 binding 不完整、receipt 无法验证或 hash 不一致时 fail closed。

## AnswerContract

```text
AnswerContract
  source_question_ref
  source_idea_set_ref
  obligations[1..N]
    obligation_key
    statement
    minimum_support
    question_trace = answer_shape + unknown_statement|applicability_scope
    idea_relevance[exactly every IdeaCandidate]
      idea_ref
      role = query_lens | experiment_lens | not_relevant
      rationale
  answer_contract_hash
```

每个 obligation 必须追溯到 Question；每个 obligation 必须恰好交代完整 IdeaSet 中的每个候选。所有 `idea_ref` 逐字使用 accepted IdeaSet 的 `candidate_key`，不得添加 IdeaSet 前缀、生成新 identity 或改写 key。Idea 只能收紧证据要求、比较结构、条件或证伪边界，不能扩张 Question。`answer_contract_hash` 在使用 Evidence 前冻结。

## Evidence 与 coverage

精确 `EvidenceRef` 绑定不可变 AssetVersion、content/manifest hash、TargetCommit root、provenance closure、capabilities、RM integrity/availability receipt 与 RG eligibility/currentness receipt。Card、preview、rank、动态 URL、mutable view、本地路径和未接纳搜索发现只用于导航。

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

每个 obligation 恰好出现一次。`covered` 至少有一个 EvidenceUse、`insufficiency = null`，并对应零个 ExperimentBrief。`gap` 必须记录 insufficiency，可以保留部分 evidence use，并至少由一个 ExperimentBrief 收口。成功的空查询可证明 gap；stale、unavailable、receipt/hash mismatch 或 outcome unknown 是技术阻塞。

## ExperimentBrief 与 disposition

ExperimentBrief 包含 Plan 内唯一 `experiment_key`、一个或多个 gap obligation keys、goal、characteristics、boundary constraints、semantic delta 与 contributing Idea refs。全部且只有 gap 必须由 Brief 覆盖。Plan 不形成 Target identity/spec、DAG、文件计划、Worker、Provider、资源或调度。

Disposition 只按内容派生：

```text
all obligations covered + no briefs -> no_new_experiment_required
any gap + every gap covered by briefs -> experiments_required
```

无 gap 只产生 Bundle skip basis candidate；Advancement Engine 决定是否形成 Bundle `StageCommit(Skipped)`。Plan Skill 不创建 Bundle Run。

## Advisory review

根 Agent 在同一 managed native Session 内以 `fork_turns="none"` spawn 一个 fresh child reviewer 并 wait。记录 `review_mode = harness_child_agent`、短命 `reviewer_agent_ref`、reviewed draft hash、findings、每条 finding 唯一的 `revised | not_adopted` disposition 与 final Plan hash。Reviewer 只提供建议，没有 RM/RG/AE authority；有 `revised` 当且仅当最终 Plan 实质改变。

## Accepted handoff

Research Memory 先接受不可变 PlanDocument；Research Graph 再形成 FormalPlan identity 与接受/拒绝 receipt。RM accepted 而 RG rejected 时保留未绑定内容，并在同一根 Session 中按正式 feedback 形成新 content identity。只有精确的 RM receipt、RG receipt、AR execution receipt 与当前 AE request/epoch 同时可验证时，AE 才能提交 Plan StageCommit。
