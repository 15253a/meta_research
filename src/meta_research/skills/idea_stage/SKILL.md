---
name: idea-stage
description: 在冻结的 accepted Question binding 上形成、评审并提交 IdeaOutcome。
---

# Idea Stage

把当前 frozen binding 转化为可供 Plan 消费的候选全集或有证据边界的负向结果。拥有研究综合、review disposition 与最终修订；把内容 custody、领域接纳和 Stage 推进留给相应 Owner。

需要核对输入、Submission 或 accepted handoff 时，读取[输入／输出契约](references/io-contract.md)；构造 Outcome、执行 advisory finalization、处理 feedback 或判断 Exhaustion 时，读取[候选与闭包契约](references/contract.md)。

## 1. 锁定输入

1. 只接收已经校验的 runtime binding、frozen ContextPack、`AcceptedQuestionBinding` 和与 binding 精确匹配的 accepted Question content。
2. 核对 Question、Quest、content 的 ref、hash、schema ref 与 RM/RG receipt 均为 exact；拒绝 `latest` 或 Agent 自造 identity。
3. binding 缺失、漂移或不可验证时返回 typed input error，不改写上游事实。
4. 只有确实缺少人类独有决定、授权或线下动作时，才请求相应 Owner 建立 typed HumanRequest。
5. Quest 最终确认已经授予该 Quest 的普通本地研究能力；可以使用 shell、工作目录与 Web Search 辅助综合，不再请求逐工具授权。
6. 宽权限不赋予 Owner authority。只在当前 research workspace 写临时研究文件；不得读取或修改 Meta-research SQLite、provider spool／seal key、control files，亦不得用 CLI 或内部文件绕过公开 Owner Interface。外部搜索发现仍只能标记为 `research_observation | unresolved`，不能冒充 accepted Evidence。

## 2. 形成一个 Outcome

1. 消费 ContextPack 中的已接纳 Evidence、active guidance、历史与 unknown boundary；新外部材料没有 Owner accepted ref/receipt 时只能是 `research_observation | unresolved`。
2. 始终分开陈述 Evidence、Agent inference 与 unknown，并记录实际消费输入的 exact provenance。
3. Idea 必须增加可检验机制、条件、干预轴或比较结构及推翻方式；指标、实验协议和冻结实验承诺留给 Plan。
4. 合并语义重复；只有会改变 Plan 研究承诺的方向才保留为独立候选。
5. 形成且仅形成 `IdeaSet` 或 `NoViableCandidate`。后者是当前 frozen closure 下可接纳的负向 Outcome，不是空集合、技术失败或 Exhaustion。

## 3. 根会合复核

1. Owner 保存 primary draft checkpoint 后，在同一 managed native 根 Session 发起第二个 provider turn，对 exact Question 与完整去重草稿执行 advisory finalization。
2. 根 Agent 重新检查 Question 对齐、实质重复、证据边界、可证伪性和 Plan 可用性；形成 bounded findings，不批准、不评分、不选 winner。
3. 对每条 finding 给出唯一 `revised | not_adopted` disposition，并返回 findings、最终完整 Outcome 与 dispositions。reviewed draft 与最终 Outcome hash 不同，当且仅当至少一条 disposition 为 `revised`。
4. 当前 review record 固定为 `review_mode=advisory_unobserved`、`reviewer_agent_ref=null`、`independent=false`。模型不报告 reviewer identity，系统也不伪造 reviewer provenance。
5. 第二个 provider turn 缺失、失败或 schema 不闭合时返回 typed blocker；不得绕过 substantive findings/dispositions 与最终 Outcome 校验。

## 4. 提交并恢复

1. 每次正式写入前重验原 invocation closure。
2. 先保存 RM content ref/receipt checkpoint，再提交 RG domain outcome；两层状态和 receipt 永不合并。
3. `rejected` 在同一根 Session 中按正式 feedback 实质修订，并以新 submission identity 绑定 predecessor 与 rejection receipt；新 Attempt 仍执行新的根 advisory finalization turn。
4. `stale | needs_input | outcome_unknown | technical_blocker` 保留原生状态；unknown 只对账原 identity，不能盲重放。
5. 一个 submission identity 只能绑定一个 immutable payload 与 invocation closure。

## 5. 谨慎提出 Exhaustion

只有 `IdeaSet` 与 `NoViableCandidate` 都无法形成或获接纳、没有可恢复 Submission/HumanRequest/blocker/unknown/未消费 accepted outcome/既有 StageCommit，且 live receipts 证明探索已经收口时，才向 AE 提交 `ExhaustionProposal`。Proposal 不冒充 Outcome 或 StageCommit。

## 收口检查

- 只交付 accepted `IdeaSetRef | NoViableCandidateRef` 与真实 Owner refs/receipts，或保留 typed recovery 状态。
- 永久保持 `execution completed != content accepted != domain accepted != Stage advanced`。
- Skill 不创建 Question、Plan、Run、receipt、canonical selected Idea 或 StageCommit，也不接纳自己的结果。
