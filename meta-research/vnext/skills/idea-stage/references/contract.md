# Idea Stage 候选与闭包契约

## 目录

- [IdeaOutcome](#ideaoutcome)
- [独立评审](#独立评审)
- [Submission 与反馈](#submission-与反馈)
- [ExhaustionProposal](#exhaustionproposal)

## IdeaOutcome

形成且仅形成 `IdeaSet | NoViableCandidate`。

### IdeaCandidate

```text
IdeaCandidate
  candidate_key
  direction
  rationale
  assumptions[]
  risks[]
  evidence_boundary:
    accepted_evidence_refs[]
    supported
    inferred
    unknown
  falsification_hint:
    test
    would_refute
  material_difference:
    from_history
    from_peers
    plan_commitment_change
```

候选表达研究语义，不表达 score 或 Owner 判断。至少明确一种 Evidence／inference／unknown 边界；`plan_commitment_change` 必须说明该候选为何导致不同的后续研究承诺。

### IdeaSet

```text
IdeaSet
  candidates[1..N]
  recommendation | none:
    note
    binding = false
```

`candidate_key` 在 Outcome 内唯一；候选必须实质不同。不设置全局数量上限、ranking、winner 或 selected candidate。

### NoViableCandidate

```text
NoViableCandidate
  exploration_scope
  candidate_families_considered[1..N]:
    family
    why_not_viable
    evidence_refs[]
  evidence_boundary
  overturn_conditions[1..N]
  why_plan_cannot_proceed
```

这是可接纳的负向 Outcome，不是空 `IdeaSet`、技术失败、review rejection 或 Exhaustion。它只约束当前 frozen invocation closure，不声称未来永远没有候选。

## 独立评审

首次提交每个 Outcome 时携带：

```text
AdvisoryReviewRecord
  review_ref
  reviewer_session_ref
  reviewed_draft_hash
  findings[]:
    finding_id
    category = question_alignment | material_duplicate | evidence_boundary
             | falsifiability | plan_usability
    message
  dispositions[]:
    finding_id
    action = revised | not_adopted
    rationale
  final_outcome_hash
```

每条 finding 恰有一个 disposition。任一 action 为 `revised` 时，final hash 必须不同于 draft hash。Reviewer 只给 findings；根 Agent 拥有 disposition 与最终 revision。Review record 不含 approval、score、selection 或 Owner authority。

Owner rejection 的 successor 另带：

```text
OwnerFeedbackRevisionRecord
  prior_review_ref
  predecessor_submission_ref
  owner_rejection_receipt_ref
  final_outcome_hash
  root_revision_rationale
  supplemental_review_ref | none
  root_owned = true
```

新 Outcome 必须实质变化并绑定真实 rejection lineage；补充 reviewer 不能替代该 lineage。

## Submission 与反馈

一个 `submission_identity` 只绑定一个 immutable normalized payload 和 invocation closure。

| Owner 状态 | 必须执行 |
| --- | --- |
| `accepted` | 分开保存 RM content checkpoint 与 RG domain receipt。 |
| `rejected` | 保存 decision receipt／feedback；在同一 feedback loop 形成实质变化的 successor identity。 |
| `stale` | 保存 receipt 并重验 exact binding；仅对 unchanged closure 做 exact replay。 |
| `needs_input` | 保存 HumanRequest／dependency 与 recovery receipt；payload 改变时建立 recovery-linked identity。 |
| `outcome_unknown` | 保存 unknown observation receipt，只对账原 identity；明确结果前不重放写入。 |
| `technical_blocker` | 保存 blocker ref／receipt；先确认无待对账副作用，再按 unchanged replay 或 changed identity 恢复。 |

RM accepted 后先持久化 content ref／receipt，再尝试 RG。所有 Owner receipt 追加保留，不被后续结果覆盖。Replacement 成为 feedback-loop head 后，旧 recoverable identity 只保留为历史。

## ExhaustionProposal

只有以下 gate 全部成立时才提交：

```text
exploration_records[1..N]
prior_submission_refs[]
owner_rejection_receipt_refs[]
cannot_form_idea_set_reason
cannot_form_no_viable_reason
pending_submission_refs[] = []
accepted_unconsumed_outcome_refs[] = []
human_request_refs[] = []
technical_blocker_refs[] = []
outcome_unknown_refs[] = []
existing_stage_commit_ref = none
defensible_idea_set_available = false
defensible_no_viable_available = false
run_reconciled = true
```

存在 Submission 或 rejection 时，分别引用 exact Submission ref 与真实 rejection receipt，并与 live history 一致。次数、分数或单次失败都不是 Exhaustion 依据。

保留 AE 的 `rejected | stale | needs_input | outcome_unknown | technical_blocker` 原生状态。Unknown proposal 只在原 proposal identity、hash 与 invocation closure 下对账。根 Agent 只提出 proposal；AE 独占 StageCommit 判断。
