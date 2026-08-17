---
name: reasoning-stage
description: 在绑定 AcceptedQuestionBinding 的 current Reasoning StageRunRequest 上综合本 Cycle、当前 Question 的跨 Cycle 进展、父 Question 与 Quest 影响，形成受证据约束的科学结论，并只提出下一 Cycle 或 Quest 完成候选。用于正常 ResearchCycle 的必经收口、负面／部分结果、证据不足、Owner rejection 后修订，以及自由选择 present + open Question 与入口 Stage；不用于接纳 Answer/Evidence、修改 Goal、编码问题依赖路由或推进 Stage。
---

# Reasoning Stage

形成当前 Question 的诚实科学结论候选，综合它对父 Question 与 Quest 的影响，再选择下一 Cycle 或提出 Quest 完成候选。此 Skill 是单一 Stage 的深模块：它消费稳定引用和 receipt，隐藏分析、选题、新 Question 创建与局部修订；它不是 Research Graph、Advancement Engine 或跨 Stage 编排器。本目录及其 fixture 是对 Stage 行为和边界的可运行 prototype，不是生产 Owner API、持久化 schema 或 receipt 实现。

## 不变量

- 只在 `StageRunRequest` 明确为 `Reasoning`、类型化、current，且冻结了由 Advancement Engine 签发的 `AcceptedQuestionBinding` 与完整输入时运行。同一 Run 内复用该 binding，不重复查询 Question currentness，也不自行复验 Formal Question 六字段 schema。request／binding 无法验证或技术 blocker 未修复时停止 Run；某项候选的必需 Owner port 无法验证时只停止该项外部提交并标记副作用 blocker。两种情况都不得伪造 acceptance、`StageCommit` 或后继 Cycle 已创建。
- 精确冻结 `upstream_stage_closure` 中 Idea／Plan／Bundle 的三个 `StageCommit`。`Skipped` 与 `Exhausted` 仍按条件化 Stage 图进入必经的 Reasoning；`plan_evidence_input = accepted | none` 明确区分真实 `FormalPlan`／`EvidenceReuseSet` 与因合法上游路径不存在该输入，禁止制造占位 Plan、空壳 Reuse Set 或把 Stage exhaustion 当作科学证据。
- `question_literature_input = revision | none` 明确区分一份可以没有 record 的诚实空 revision 与没有 revision；`none` 不能靠缺字段暗示。每个 LiteratureRecord 保留实际 `evidence_basis` 与 basis ref，snippet、citation context、abstract 或不可读记录不得被升级成已验证全文证据。
- 每个已接纳的 `TargetCommit` closure 都独立保留 `Target → Baseline → Variant → VariantRun → Evaluation → ProtocolVersion → selected EvaluationAttempt → MetricResult`、恰好一个 selected Attempt 与 MetricResult、`0..N` 个 selected `CheckpointArtifact`、两份 `ExecutionInputBinding`、RM／RG／AR receipt 以及 selected Log／Analysis。比较多个 closure 时逐份保持来源；空缺回到其 Owner 或后继 Cycle 获取，不能用所谓 latest Attempt 补齐、把不同 Attempt 拼成一份测量，或把 Log／Analysis 当作 MetricResult。
- 每个可运行的 Reasoning 都形成一个面向当前 `QuestionRef` 的 `ScientificOutcomeCandidate`：`affirmed`、`denied`、`uncertain` 或 `insufficient_evidence`。`uncertain` 表示最低必需证据已经具备、但适格结果本身不收敛；`insufficient_evidence` 表示仍缺回答所必需的证据。两者都是诚实候选，不是技术失败或 `Exhausted`。
- 科学结论的 disposition 不自动映射为 `QuestionResearchState`。`open | resolved | dead_end` 的接纳、转换与 receipt 仍受 `TODO-CONTRACT(#64: Answer/Evidence 接纳与 QuestionResearchState 转换语义尚未决定)` 约束，Reasoning 不自行改写。
- 每次正常收口还必须形成且只形成一个 `ReasoningTransitionCandidate = NextCycleProposal | CandidateCompletion`。它与同一 `ScientificOutcomeCandidate` 的 request、Foreground Epoch、Session、来源 Question 与 Quest 精确绑定，并只投影允许字段；前者继续研究，后者仅在判断 current Quest Goal 与完成里程碑已经满足时提出，两者都不产生权威状态变化。
- `execution completed ≠ Answer/Evidence accepted ≠ CandidateCompletion user-confirmed ≠ Goal/Completion Owner accepted ≠ Quest ended ≠ Stage advanced`。本 Skill 从不接纳自己的候选，也不形成 `StageCommit`。Reasoning 没有 `Exhausted` 路径。

## 输入门禁

执行或校验任意实际／fixture Run 时，先读取 [`references/contract.md`](references/contract.md) 的完整输入闭包、候选形状与失败矩阵；只解释概念而不处理 Run 时无需加载它。

1. 验证 request 的 identity、Stage=`Reasoning`、Foreground Epoch、冻结输入和 currentness；验证其中 `AcceptedQuestionBinding` 的 receipt、`QuestionAnchor`、content ref/hash 与 request 的精确 `QuestionRef` 一致。把无法证明的值视为不 current，并在整个 Run 内复用这份 binding。
2. 验证 `upstream_stage_closure` 恰好按 Idea → Plan → Bundle 绑定三个不可变 StageCommit。每个 `Skipped` 必须有类型化依据；至多一个 `Exhausted`，其后的可选 Stage 只能是引用该 exhausted commit 的 `Skipped`。按 [`references/contract.md`](references/contract.md) 校验 `plan_evidence_input` 与真实路径一致；无 Plan 输入的路径也必须进入 Reasoning。
3. 验证每个复用叶子与本轮结果都有精确来源及角色。`accepted_target_commit_closures` 必须满足完整语义链、唯一 Attempt／MetricResult、两份 execution binding、receipt 分权与 checkpoint／log／analysis selection；closure 不完整或角色不匹配时 fail closed。Log／Analysis 可供解释，但不能冒充测量。
4. 验证 `question_literature_input` 的显式分支。`revision` 保留每条 record 的 `ref + evidence_basis + evidence_basis_ref` 及可选 `reading_result_ref`，record 数量可以为零；`none` 不允许任何文献 evidence leaf。
5. 读取冻结的 Research Context：本 Cycle、当前 Question 的既有 accepted outcome refs、可选父 Question 链、当前 ActiveGraphSnapshot 和 Quest Goal revision。它是分析上下文，不是证据全集、权限白名单或新的聚合 Owner。
6. 区分运行阻塞与科学覆盖不足。若冻结输入无效／不 current、承诺中的外部结果仍 pending／unknown、Owner submission 未决或 Capability／receipt 不可验证，报告可观察 blocker 给其 Owner 并停止该项提交。只有所有冻结输入都有效且处于终态、但其科学覆盖仍不足以回答 Question 时，才形成 `insufficient_evidence`。

需要可运行的 fixture 与边界检查时，使用 [`scripts/reasoning_stage_mvp.py`](scripts/reasoning_stage_mvp.py)。其 fake 只验证行为，不产生正式 Owner fact 或 receipt。

## 形成科学结论候选

1. 将每条证据标注为复用、本轮 accepted 结果、负结果、部分结果或分析解释，并保留精确 ref、语义角色与来源闭包。Evidence Reuse leaf 保留其 AssetVersion、source TargetCommit／VariantRun／EvaluationAttempt、精确 role-source binding、provenance、capability、资格／完整性／可用性／当前性与角色接纳 receipt，以及 supported claim／support boundary；本轮 leaf 只能引用完整 TargetCommit closure 中同角色对象。所选 Log／Analysis 保留其来自本链 VariantRun 或 selected EvaluationAttempt 的来源绑定；LiteratureRecord 保留原始 evidence basis。Log／Analysis／Checkpoint 只用于其真实角色的解释、限制、溯源或复现；`affirmed | denied | uncertain` 至少引用一份冻结的 LiteratureRecord 或 MetricResult。
2. 先依据当前 Question 的 `answer_shape`、`applicability_scope` 和冻结研究输入判断最低必需证据是否齐备。若存在必需缺口，选择 `insufficient_evidence`：令 `claim = null`、`missing_evidence` 非空、`uncertainty_basis` 为空，并精确列出缺口。若最低必需证据齐备，令 `missing_evidence` 为空，再进入结果判定。此处“什么证据是必需的”由 Reasoning 按研究语义裁定；fixture 只检查输出自洽。
3. 对证据齐备的 Question 写出最窄的 `claim`，并使方向服从冻结 Question 的 `unknown_statement` 与 `answer_shape`：`affirmed` 给出证据支持的肯定方向答案，`denied` 给出证据支持的否定方向答案，两者都令 `uncertainty_basis` 为空；若适格结果彼此冲突、跨越既定判定阈值或在既定条件下不具区分力，选择 `uncertain` 并写入非空 `uncertainty_basis`。额外证据可能提高置信度但并非当前 claim 的必需条件时，把它记入 `limitations`，不要伪装成必需缺口。fixture 验证字段与证据闭包，Answer/Evidence Owner 才能接纳 claim 的科学方向与充分性。
4. 写入 `support_scope`、`limitations`、上述 disposition 专属字段、`causal_interpretation` 和带 finding 的角色化证据闭包。多个变化轴不触发机械降格，也不自动允许单轴归因；在 `causal_interpretation` 原样保留相关 Target refs、全部 changed-axis facts、held-fixed facts、provenance 与 attribution basis。框架只验证来源闭包、角色和判定形状，不能代替 Reasoning 裁定科学充分性。
5. 若输入冻结了 `bundle_replan_candidates`，逐个在 `bundle_replan_interpretations` 中精确引用 candidate、source Bundle StageCommit 与完整原始 basis refs，只补充其如何限制本轮结论或影响后继 Cycle。`replan_required` 只能由 Bundle 提出；Reasoning 不得创建／改写 candidate，也不得输出本地 `replan_required: bool`。技术错误、资源问题或缺 receipt 仍走 blocker／fail-closed，不冒充语义重规划。
6. 自主完成一次多尺度综合：说明本 Cycle 如何改变当前 Question 的跨 Cycle 解决进度；对冻结 parent Question 链中的每一项分别给出实质影响、无实质影响或未知；再说明对 current Quest Goal／完成里程碑的影响。具体分析维度由研究语义决定，不能证明的影响保持未知。完成条件是冻结的 Cycle、当前 Question、每个 parent Question 与 Quest scope 都已被覆盖。
7. 生成 `ScientificOutcomeCandidate`，其身份绑定当前 `StageRunRequestRef`、`AcceptedQuestionBindingRef`、`QuestionRef` 与冻结 Research Context；综合分析只解释影响，不修改 Question 状态、父子关系、Goal 或 Quest。

## Owner feedback 与 fail-closed 提交

先在本 Run 内形成并验证非权威候选；只有环境提供可验证的具名语义 port 时才执行外部提交。port 未实现或不可验证会阻止该项副作用与 acceptance 声明，但不会把一个已通过本地门禁的候选伪装成失败或权威事实。生产实现、持久化、receipt 和 lifecycle 均不在此 Skill 内。

- `submit_answer_candidate(candidate)` — `TODO-CONTRACT(#64: Answer/Evidence identity、ConclusionCommit、ScientificValidity、revalidation 与 Question revisit 的精确提交及反馈语义尚未决定)`。首次副作用前必须由原 `StageRunRequest` 重建候选并与待提交值精确相等，再验证 port、身份、currentness 与预期返回契约；调用返回后才验证 feedback receipt 与同一 request／Question／Session 的绑定。
- `submit_confirmed_completion_candidate(candidate, user_confirmation_receipt_ref)` — Reasoning 先把候选交给用户；只有明确用户确认 receipt 存在时，外部协作层才可将候选提交给 Goal/Completion Owner。`TODO-CONTRACT(#89: 精确确认绑定、Owner operation／receipt、拒绝后的继续／改向／reopen 与 AE 结束迁移语义尚未决定)`。用户确认或 Owner acceptance 单独都不结束 Quest。
- Owner `rejected` 且同一 request／Session 仍 current 时，读取 rejection receipt，把规范候选的隔离副本交给修订逻辑，按反馈修正 disposition、claim、scope 或限制后形成新 revision 并重提。revision 必须绑定该 rejection receipt，冻结 request／Epoch／Question／Quest／Goal／输入与证据身份，并重新通过 disposition、因果解释与综合字段门禁；原地修改隔离副本也不得改变比较基准，不得把 revision 回调当成绕过验证的第二入口。`stale`、currentness 未知、冲突 receipt 或 revision 校验故障使该 revision fail closed，后继不能绑定无效 revision；revision 已通过门禁但重提 port 不可用时，只停止重提副作用并报告 blocker，该 revision 仍是本 Run 的最终本地候选。
- Runtime 的 completion 仅是运行事实；它不能替代 Answer/Evidence 或 Goal receipt，也不能触发 Stage advancement。

## 选择唯一后继

以本 Run 中最终通过门禁的 `ScientificOutcomeCandidate`（包含已完成的可验证 rejection revision）形成且只形成一种非权威后继候选；它不等待或冒充 Answer/Evidence acceptance：

- `NextCycleProposal`：自由选择 current Quest 中任意正式 Question，绑定其稳定 `QuestionAnchor`，以及同一 Quest／图修订上 current 的 `GraphPresenceFact=present` 与 `QuestionResearchStateFact=open`；再建议 `entry_stage = Idea | Plan | Bundle | Reasoning`。later-stage 入口按每个被跳过的 Stage 携带 `typed_basis_refs`，其真实性与适用性由 Advancement Engine 验证。Skill 只投影 Anchor／selection fact 的规范字段，不读取或输出 Question 依赖／阻塞路由；`active` 只由 Foreground Cycle 派生。
- 继续当前 Question 与切换其他既有 Question 只改变目标引用。需要新建或分解 Question 时，先校验 entry Stage、每个 skip basis、自治 mode 与分解输入，再把 source 精确绑定到 current Reasoning `StageRunRequest`、Foreground Epoch、来源 Question 与 Quest，并以 `creation_mode = AutonomousCreation`、`mode = new | decompose` 调用高层生命周期入口 `create_question(direction)`；任何本地校验失败都必须发生在创建副作用之前。Reasoning 永远不能从此入口启动或转换为 ManualCreation。分解必须绑定 parent Question 与明确依据。
- `create_question` 继续同一 AutonomousCreation 的 CreationContext／强制 DeepFetch／QuestionCommit；可恢复等待继续等待并恢复同一创建流程，Reasoning 不直接构造 QuestionCommit 或创建 Question identity。`TODO-IMPL(question-creation.create_question; source=#85,#105)`。只有该入口完成并返回 RG 已接纳的 `QuestionAnchor` 与上述两项可选择事实后才能形成 `NextCycleProposal`；草稿、local id、内部创建方向或 `QuestionProposalRef` 只属于防泄漏边界，不是正常后继分支。新建 Question 可以使用创建后的图修订事实，不要求它出现在本 Run 开始时冻结的旧 ActiveGraphSnapshot。
- `CandidateCompletion`：只在综合分析认为 current Quest Goal 与完成里程碑已满足时提出，并绑定精确 Goal revision 与完成里程碑依据。Reasoning 只形成候选；用户明确确认后，Goal/Completion Owner 才能接纳，随后仅 Advancement Engine 的结束迁移能使 Quest 真正结束。精确 receipt 与 operation 仍受上述 `TODO-CONTRACT(#89)` 约束。
- Advancement Engine 消费 `NextCycleProposal` 时重新验证目标 Question、状态、入口 Stage 与 skip basis，随后才创建 successor Cycle 并为新 Run 冻结 `AcceptedQuestionBinding`；Reasoning 不直接启动 Cycle。

## 待 #104 Resolution 正式化的 supersession

当前 prototype 的唯一 outward transition 是一项待 #104 Resolution 正式接纳的替代，不把尚未发布的设计冒充现行 Resolution：

- 用一个 `NextCycleProposal` 替代 #100 中 Reasoning 对外分离的 `SelectProposal + CycleStartProposal`；正常收口与同一 `ScientificOutcomeCandidate` 绑定且恰好产生一个 `NextCycleProposal | CandidateCompletion`。
- `QuestionProposal` 留在 `create_question` 的完整 AutonomousCreation 生命周期内部；Reasoning 只有拿到正式 `QuestionAnchor + current present/open facts` 才能对外形成 `NextCycleProposal`。
- `QuestProposal` 不再是 Reasoning outward transition。独立 Quest 的创建／接纳生命周期不由本票替代，也不得由 fixture 自行发明；若仍需要该能力，应由其适用 Owner 合同另行决定。
- Question 的选择资格从旧候选形态收敛为 current Quest 视图中的 `present + open`。`QuestionResearchState` 的精确生产 schema、currentness、接纳和转换 receipt 仍是 `TODO-CONTRACT(#64)`；fixture 中的 fact 名称只是非约束性测试编码。

## 完成边界

在当前 Session 内，交付可审核的科学结论候选、覆盖全部冻结 scope 的多尺度综合，以及恰好一个 `NextCycleProposal | CandidateCompletion`；若外部提交 port 不可用，同时交付该副作用 blocker。只有输入／currentness／闭包本身无法通过门禁或承诺中的结果仍未决时，才以明确的 fail-closed blocker 代替候选。完成不表示 Science／Goal 被接纳，也不表示 Cycle 已创建、Quest 已结束或 Stage 已推进；相应用户确认、Owner acceptance 与 Advancement Engine 迁移必须分别成立。
