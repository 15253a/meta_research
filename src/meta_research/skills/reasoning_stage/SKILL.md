---
name: reasoning-stage
description: 在 current Reasoning StageRunRequest 的冻结 Question、路线闭包和证据上形成非权威 ScientificOutcomeCandidate，并恰好提出一个 NextCycleProposal 或 CandidateCompletion。用于每个正常 Cycle 的必经收口；不用于接纳候选、创建 Question/Cycle、结束 Quest 或推进 Stage。
---

# Reasoning Stage

把一个由 Advancement Engine 与 Agent Runtime 已冻结、已围栏的 Reasoning invocation closure 收口为诚实科学候选。开始前完整读取[语义合同](references/contract.md)和[Owner 操作](references/owner-operations.md)。

## 1. 先证明调用仍 current

1. 只使用本 Run 的 scope-bound resident Semantic MCP。第一项调用必须是 `advancement_engine.reasoning_stage_run.observe`，核对 exact request、Run、Attempt、Fence、Question、Quest、Foreground epoch 与 runtime binding。
2. 再调用 `research_memory.reasoning_evidence.read` 和 `research_graph.reasoning_context.read`，重建冻结内容／角色与 current domain context；禁止用 latest、workspace、搜索结果、UI 卡片或模型记忆补齐。
3. 任一 MCP credential、operation、currentness、receipt、hash、角色或终态不可证明时 fail closed，不输出成功候选。

## 2. 保持路线与证据语义

1. Idea／Plan／Bundle 的 Completed、Skipped、Exhausted 与 NoViableCandidate 都只是 exact upstream route closure；它们不自动成为科学证据或 Quest completion。
2. `QuestionLiteratureRevision | none` 和 `Plan evidence | none` 是显式分支。不存在就是不存在，禁止空壳 Plan、默认 revision 或 `not_applicable` 占位。
3. LiteratureRecord 与 MetricResult 可以提供 substantive scientific support。若 accepted FormalPlan 选择了 Baseline Pool 证据，只能消费 AE 冻结的 `EvidenceReuseLeaf(role=MetricResult)`：它必须由 RG 经 TargetCommitEvidenceAuthority 对 exact Plan StageRunRequest catalog、selected use 与 RM/RG receipts 重建，禁止按 EvidenceRef 改读 latest。LogAsset、AnalysisAsset、CheckpointArtifact 只保留诊断、解释、状态或复现角色。

## 3. 形成一个 ScientificOutcomeCandidate

只返回 `affirmed | denied | uncertain | insufficient_evidence` 之一：

- `affirmed | denied`：有 closure 内 substantive evidence、非空 bounded claim、无 missing evidence 与 uncertainty basis。
- `uncertain`：最低证据已具备但适格结果不收敛；有 substantive evidence、非空 claim 与 uncertainty basis，无 missing evidence。
- `insufficient_evidence`：回答所需证据确实缺失；`claim=null`、missing evidence 非空、uncertainty basis 为空。

候选精确绑定 StageRunRequest、Cycle、Question、Quest、Goal revision 与 Foreground epoch，并始终 `is_authoritative=false`。

每个候选还必须完整填写 `support_scope`、`limitations`、`causal_interpretation` 与 `research_synthesis`。后者逐项覆盖本 Cycle、current Question 的所有 prior accepted outcomes、冻结 parent chain 中的每个 Question，以及 Quest 的 exact Goal/graph revision。因果部分必须逐项复用冻结 context 的 TargetCommit、changed-axis、held-fixed 与 provenance refs；不得自行补写 latest 或未签发 ref。

## 4. 恰好一个 outward transition

- `NextCycleProposal` 只引用已接纳的 target Question 与 QuestionAnchor；同时显式给出 `entry_stage` 和恰好覆盖其前序 Stage 的 `typed_skip_basis_refs_by_stage`。Idea 入口的 map 为空；任何 skip ref 都必须是对应 Owner 可重验的 typed basis，不能把 ScientificOutcome 冒充 IdeaSet／FormalPlan。它不是 Question/Cycle creation receipt。
- source-current 的历史 root/manual Question 以其稳定 `question_ref` 作为 anchor ref；RG 仍须独立签发并 current 重验 `GraphPresenceFact=present` 与 `QuestionResearchStateFact=open`，不得把 lifecycle `active` 当作二者。
- `CandidateCompletion` 只绑定 current Quest、exact Goal revision、明确 milestone basis 与 rationale；它不是用户确认、RG acceptance 或 AE ending transition。
- 两项必须 XOR。Reasoning 不输出 QuestionProposal，不把 autonomous lifecycle 的 draft/local id 泄漏成后继。

## 5. Root advisory finalization

Owner 保存 primary draft 后，在同一 managed native root Session 的第二个 provider turn 重新核对三项 resident Semantic MCP，并检查 source/currentness、证据角色、disposition、唯一 transition 或 internal Autonomous scope 与 Owner 边界。根 Agent 形成 bounded findings，对每条 finding 给出唯一 `revised | not_adopted`，`revised` 必须实际改变闭合 output。Autonomous checkpoint 与 creation 后 resume 都使用同一 `advisory_unobserved`、null reviewer、`independent=false` 形状；不声称未观测的 reviewer provenance。

## 收口边界

永久保持：`provider completed != content accepted != domain accepted != Stage advanced`，以及 `CandidateCompletion != human confirmed != RG accepted != Quest ended`。Skill 不调用候选写入效果；RM/RG/AR/AE daemon 在各自公开 Interface 上独立接纳、对账与推进。
