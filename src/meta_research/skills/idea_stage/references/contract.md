# Idea Stage 候选与闭包契约

## IdeaOutcome

形成且仅形成 `IdeaSet | NoViableCandidate`。

`IdeaCandidate` 必须包含唯一 `candidate_key`、`direction`、`rationale`、非空 `assumptions` 与 `risks`、Evidence/inference/unknown 三分的 `evidence_boundary`、`falsification_hint`，以及说明其如何改变 Plan 承诺的 `material_difference`。候选不表达 score 或 Owner 判断。

`IdeaSet` 包含一个或多个实质不同候选；可带 `binding = false` 的 recommendation，但不得 ranking、winner 或 selected candidate。

`NoViableCandidate` 必须记录探索范围、已考虑候选族及为何不可行、Evidence boundary、推翻条件和 Plan 当前为何不能负责地继续。它只约束当前 frozen closure。

## 独立评审

首次提交携带 advisory review：独立 `reviewer_session_ref`、reviewed draft hash、分类为 `question_alignment | material_duplicate | evidence_boundary | falsifiability | plan_usability` 的 findings、每条 finding 唯一的 `revised | not_adopted` disposition，以及 final outcome hash。Reviewer 没有 Owner authority。

Owner rejection 的 successor 必须绑定 predecessor submission、真实 rejection receipt 和根 Agent 的修订，并产生实质不同的 Outcome hash。

## Submission 与反馈

一个 submission identity 只绑定一个 immutable normalized payload 与 invocation closure。RM accepted checkpoint、RG decision、AR completion 和 AE commit 分别持久化。`rejected` 产生有 lineage 的 successor；`stale` 重验 exact binding；`needs_input` 等待精确依赖；`outcome_unknown` 只对账；`technical_blocker` 先证明无未知副作用再恢复。

## ExhaustionProposal

只有 pending submission、accepted unconsumed outcome、HumanRequest、technical blocker、outcome unknown 和 existing StageCommit 均为空，Run 已对账，且 `IdeaSet` 与 `NoViableCandidate` 都不可辩护时才能提出。次数、分数或单次失败不是 Exhaustion 依据；AE 独占是否形成 StageCommit 的判断。
