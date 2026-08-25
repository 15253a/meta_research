# Reasoning Stage 生产语义合同

## Invocation closure

`ReasoningSkillRequest` 绑定 current StageRunRequest、Run、Attempt、Fence、root Session、Cycle、Question、Quest、Goal revision、Foreground epoch、ContextPack ref/hash、完整 frozen evidence closure 与 ReasoningRuntimeBinding。ContextPack 保持 AE 签发的 accepted Question binding、`QuestionLiteratureRevision | none`、Idea→Plan→Bundle StageCommit closure、`Plan evidence | none`、accepted TargetCommit closures 和 research context。Accepted Plan evidence 恰含 FormalPlan binding、原样 `evidence_reuse_set` 与逐 EvidenceRef 的 `EvidenceReuseLeaf/v1`；每个 leaf 绑定 exact catalog entry/use hashes、MetricResult、EvaluationAttempt、TargetCommit 以及 RM asset、RG role/formal-measurement/TargetCommit receipts。

Adapter 在启动 provider 前验证静态 identity/hash/closure；provider 必须通过 scoped Semantic MCP 重新观察 current AR/AE scope。未知 currentness 不等于 current。

## Closed output

顶层恰含：

```text
schema_ref = meta-research/reasoning-stage-output/v1
scientific_outcome: ScientificOutcomeCandidate
next_cycle_proposal: NextCycleProposal | null
candidate_completion: CandidateCompletion | null
```

后二者恰有一项非 null。ScientificOutcomeCandidate 与 transition 都绑定同一个 source StageRunRequest、Cycle、Question、Quest、Foreground epoch 和 scientific outcome identity，并且 `is_authoritative=false`。

Scientific evidence citation 恰含 `kind + ref + finding`，只能引用 frozen closure 中同 ref、同角色的 leaf。Plan reuse 引用 leaf 内 `metric_result_ref`，不能引用 EvidenceRef、AssetVersion 或诊断 receipt。`affirmed | denied | uncertain` 至少有 LiteratureRecord 或 MetricResult；诊断资产不能替代它们。disposition 的 claim／missing／uncertainty 形状由 `reasoning_contract.py` 的公开 closed validator 统一裁决。

ScientificOutcome 还闭合 `support_scope`、`limitations`、`causal_interpretation` 和四尺度 `research_synthesis`。AE 冻结 RG public issuer seam 返回的 graph revision、active Question refs、完整 parent chain 与 current Question 的 prior accepted outcomes，并同时冻结 current Cycle、Quest Goal revision、三项 upstream StageCommit 及 Target causality refs。provider schema 固定这些 identity/集合；RM 与 RG 都用同一 validator 重验，禁止在恢复时改读 latest。

`NextCycleProposal/v1` 还必须闭合 `entry_stage = idea | plan | bundle | reasoning` 与 `typed_skip_basis_refs_by_stage`：map 的 key 恰好是 entry 之前的 Stage，每个 value 是非空、去重的 typed basis ref 列表。最终 proposal 的 route 必须逐项复用 Autonomous checkpoint 已审查的 route；RG 在接纳 transition 时重验 target Question 的 issuer-owned Anchor、present/open facts 与每个 basis。Reasoning scientific outcome 只授权 autonomous absent-input skip；不能替代目标 Question 的 AcceptedIdeaSet 或 FormalPlan。

## Closed review

Review response 恰含 schema ref、fresh child identity、findings、完整 final output 与 dispositions。每条 finding 恰有一条同 id disposition；`revised` 当且仅当 final output hash 与 reviewed draft hash 不同。Review advisory-only，不能批准科学语义。

## Fail-closed matrix

| 观察 | 结果 |
| --- | --- |
| 无 resident MCP authority/endpoint/credential | provider 前停止 |
| full-conformance 缺任一 Reasoning operation | provider 前停止 |
| channel operation binding 缺失或与 catalog 不同 | provider 前停止 |
| `reasoning_stage_run.observe` 缺失或不是 read/verify | provider 前停止 |
| 任一未来 effect 缺 matching reconcile binding | effect 前停止；本版本不授予 Agent-callable effect |
| provider trace 未先观察 currentness，或未完成三项 read | 不接纳 provider output |
| ContextPack/evidence hash、source binding 或 closed schema 不一致 | 不形成 Skill result |
| primary/review native Session 改变或 child trace 不成立 | 不形成 Skill result |
| durable provider outcome unknown | 保留原 operation/channel 进入 reconciliation，不重放 |

技术 blocker 不得重写成 `insufficient_evidence`。后者只表示所有冻结输入均有效且终态，但科学覆盖仍不足。

## Prototype Delta

固定 commit `f2d3f3f0d77a6f50ab535d50d6d404a525c09757` 的 fixture 使用了开放式 synthesis、causal interpretation、selection facts 与 fake semantic ports 来验证行为。生产实现保留其科学语义，但把 synthesis/causal 字段收敛为 `reasoning_contract.py` 已版本化的 closed ScientificOutcome，并把每个尺度绑定到 AE 冻结、RG issuer-verified 的 context；AE/RM/RG 三项真实 read 替代 fake currentness；同一 native root Session 内的 child identity 替代 fixture reviewer session。AutonomousCreation 的高层 effect 与 Quest-ending 写链由对应 Owner/daemon 独立完成，Reasoning adapter 只在拿到已接纳 QuestionAnchor 后输出 NextCycleProposal，也不把部分 creation state 暴露成第二分支。
