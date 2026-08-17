# Plan Stage 合同

本文件是 AnswerContract、证据覆盖、PlanDraft、独立审阅、FormalPlan、Bundle 跳过与 ExhaustionProposal 的语义来源。

## 目录

- [调用闭包](#调用闭包)
- [AnswerContract 候选](#answercontract-候选)
- [证据查询与引用](#证据查询与引用)
- [覆盖与复用](#覆盖与复用)
- [ExperimentBrief 与 FormalPlan](#experimentbrief-与-formalplan)
- [审阅记录](#审阅记录)
- [接受与跳过依据](#接受与跳过依据)
- [耗尽提案](#耗尽提案)

## 调用闭包

```text
PlanStageInvocation
  stage_request_ref
  validated_runtime_binding_ref
  execution_fence_ref
  foreground_epoch_ref
  context_pack_ref + context_pack_hash
  accepted_question: AcceptedQuestionBinding + exact content data
  accepted_idea_set: AcceptedIdeaSetBinding + complete IdeaSet data
  search_boundary_ref + search_boundary_hash
```

`PlanStageRunRequest` 冻结以上来源绑定和完成合同。候选 AnswerContract 由 Plan 主 Agent 在根 Session 内推导，并随 FormalPlan 一同交给 Research Graph 接受。

`AcceptedQuestionBinding` 保存精确的 `QuestionRef`、内容 ref／hash／schema ref、RM 内容回执、RG Question 回执和 StageRun 当前性绑定。Plan 读取已接受的 unknown、answer shape 与 applicability scope。

`AcceptedIdeaSetBinding` 保存精确的 `IdeaSetRef`、内容 ref／hash、RM 内容回执、RG IdeaOutcome 回执、Idea StageCommit ref 和完整 IdeaSet 数据。IdeaSet 保持集合身份；Plan 在 obligation 层逐项表达 Idea relevance。

## AnswerContract 候选

```text
AnswerContract
  source_question_ref
  source_idea_set_ref
  obligations[1..N]:
    obligation_key
    statement
    minimum_support
    question_trace[1..N] = unknown_statement | answer_shape | applicability_scope
    idea_relevance[exactly every IdeaCandidate]:
      idea_ref
      role = query_lens | experiment_lens | not_relevant
      rationale
  answer_contract_hash
```

每个 obligation 必须追溯到 `answer_shape` 和至少一个其他 Question 语义字段。Idea 只能收紧“什么算作证据”，不能扩张 Question。

- `query_lens`：把机制、条件、干预轴、对照结构或证伪边界加入复用证据查询。
- `experiment_lens`：参与新证据设计，不约束复用查询。
- `not_relevant`：记录本 Plan 内的局部理由。

矩阵必须覆盖完整 IdeaSet，但不生成实验的笛卡尔积。没有贡献的 Idea 仍被显式交代；这既不是拒绝，也不形成 canonical selection。

在打开搜索快照前冻结 hash。obligation、trace、Idea role 或 rationale 任一变化，都产生新的合同 hash 和查询闭包。

## 证据查询与引用

```text
EvidenceQuery
  stage_request_ref
  answer_contract_hash
  obligation_key
  statement + minimum_support
  idea_lenses[]
  search_snapshot_token

EvidenceRef
  evidence_ref
  asset_version_ref
  target_commit_root_ref
  provenance_closure_refs[1..N]
  capabilities[1..N]
  eligibility_token_ref
  integrity_receipt_ref
  availability_receipt_ref
  currentness_receipt_ref
```

搜索快照稳定遍历过程；科学有效性仍由主 Agent 判断。提交前只重新验证入选叶子。Card、preview、rank、动态 URL、mutable view 和本地路径属于导航材料；精确 EvidenceRef 必须绑定不可变 AssetVersion、TargetCommit root、provenance closure、capability 及资格、完整性、可用性、当前性证明。

Evidence capability 可扩展。MetricResult、LogAsset、已接受的图／表／分析、trace、sample 或后续已接受的代码产物，只要精确能力和解释边界明确，都可成为 EvidenceRef。

## 覆盖与复用

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
  insufficiency | none
```

一个 EvidenceRef 可以支持多个 obligation，一个 obligation 也可以组合多个 ref；每个映射都必须说明支持内容和停止边界。部分证据可以保留在 `gap` 判定中。

`covered` 至少包含一个 evidence use，并对应零个 ExperimentBrief。`gap` 包含 insufficiency 说明和至少一个 ExperimentBrief。成功返回空集的查询可以证明证据缺口；不可用、过期或结果未知的查询属于技术阻塞。

## ExperimentBrief 与 FormalPlan

```text
ExperimentBrief
  experiment_key
  gap_obligation_keys[1..N]
  goal
  characteristics
  boundary_constraints
  semantic_delta
  contributing_idea_refs[]

FormalPlan
  exact AnswerContract
  EvidenceReuseSet
  CoverageDecision[exactly every obligation]
  GapSet
  ExperimentBrief[]
  IdeaTrace
  review_record
  source bindings
```

`ExperimentKey` 只在当前 FormalPlan 内稳定。Characteristics 描述实验是什么及关键因果变化和比较形式；BoundaryConstraints 描述可变量、固定项及必需的数据／Metric／测量条件；SemanticDelta 描述 Baseline／Variant／Evaluation 因果轴。

Target identity、Target spec、DAG、实现路线、文件计划、Worker、Provider、资源和调度属于 Bundle。FormalPlan 保存实验语义，不取得这些权限，也不创建 `SelectedIdea`。

Bundle disposition 只能按下式派生：

```text
all obligations covered + no briefs -> no_new_experiment_required
any gap + every gap covered by briefs -> experiments_required
```

## 审阅记录

```text
AdvisoryReviewRecord
  reviewer_session_ref
  reviewed_draft_hash
  findings[]
  dispositions[exactly every finding]: revised | not_adopted + rationale
  final_plan_hash
```

审阅者只检查 Question 对齐、完整 Idea 交代、Evidence 支持边界、obligation 覆盖、gap 到 Brief 闭包和权限边界。审阅记录不含 approval、veto、Owner receipt 或 acceptance authority；主 Agent 最多执行一次修订。

## 接受与跳过依据

Research Memory 先接受不可变 PlanDocument 内容。Research Graph 再接受 FormalPlan 身份，以及精确 Question／Cycle／StageRunRequest／内容绑定、AnswerContract、证据关系、ExperimentKeys、Bundle requirement、有效性和当前性。

```text
BundleSkipBasisCandidate
  formal_plan_ref
  answer_contract_hash
  disposition = no_new_experiment_required
  rm_plan_content_receipt_ref
  rg_formal_plan_receipt_ref
  currentness_observation_ref
```

`BundleSkipBasisCandidate` 是 Advancement Engine 的验证输入。两份 Owner 回执存在且所有绑定与证据当前性已知时，它才有效；`StageCommit` 仍由 Advancement Engine 形成。

## 耗尽提案

`ExhaustionProposal` 必须包含精确探索记录、提交与拒绝 lineage、合同 hash、无有效候选的证明，并证明 pending submissions、HumanRequests、technical blockers、unknown outcomes、accepted unconsumed Plans 和 existing StageCommit 均为空。

计数、耗时、资源耗尽、证据服务故障或单个草案被拒绝只描述执行处境，不能证明语义耗尽。
