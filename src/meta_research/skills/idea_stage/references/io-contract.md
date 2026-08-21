# Idea Stage 输入／输出契约

## IdeaStageInvocation

调用信封包含 exact `stage_request_ref`、validated runtime binding、ContextPack ref/hash、`AcceptedQuestionBinding` 与匹配的 content data，以及 Quest goal、文献、历史、Evidence 和 active guidance 的精确 binding。所有 ref/hash/schema/receipt 必须 immutable 且跨信封一致。

`AcceptedQuestionBinding` 包含 Question/Quest/content ref、content hash/schema ref、RM content accepted receipt 与 RG Question accepted receipt。Question 内容作为另一个与 ref/hash 精确匹配的数据对象交付；Idea 只消费其语义，不拥有 Question schema 或生命周期。

## IdeaSubmission

Submission 包含唯一 identity、Stage request、runtime binding、ContextPack、AcceptedQuestionBinding、实际 consumed/discovered inputs、`IdeaSet | NoViableCandidate`、Outcome hash、独立 review record 和 lineage。未被 Owner 接纳的新材料不能标记为 Evidence。

## Accepted handoff

Plan 只接收完整 handoff：accepted IdeaOutcome ref、RM content ref/receipt、RG domain accepted receipt、AR run execution completed receipt 与 AE StageCommit ref。Draft、review finding、Session 状态、隐藏推理、rejection、unknown 或 ExhaustionProposal 都不是 accepted handoff。

## 验收边界

Submission、Owner accepted outcome 和 Stage handoff 是三个不同对象；RM/RG receipts 始终分开；StageCommit 只能作为外部 Owner 接纳结果进入 handoff。
