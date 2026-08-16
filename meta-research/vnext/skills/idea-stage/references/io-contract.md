# Idea Stage 输入／输出契约

只定义 Idea 自有的调用信封、Submission 与 accepted handoff。调用者必须先提供已经校验的 runtime binding；本契约不展开共同运行机制。

## IdeaStageInvocation

```text
IdeaStageInvocation
  stage_request_ref
  validated_runtime_binding_ref
  context_pack_ref
  context_pack_hash
  accepted_question: AcceptedQuestionBinding + bound content data
  quest_goal_binding
  literature_binding = none | exact binding
  prior_accepted_bindings[]
  active_guidance_bindings[]
```

所有 ref、hash、schema ref 与 receipt 都必须 exact；拒绝 `latest`、聊天消息 id 和 Agent 自造的可变 identity。ContextPack 必须与 request、runtime binding 和下列 Question binding 指向同一 immutable closure。

## AcceptedQuestionBinding

```text
AcceptedQuestionBinding
  question_ref
  quest_ref
  question_content_ref
  question_content_hash
  question_content_schema_ref
  rm_content_accepted_receipt_ref
  rg_question_accepted_receipt_ref
```

这是已接纳上游事实的 opaque 信封。调用者以内联 data 或经 `question_content_ref` 校验的 data 交付 exact content；Idea 可消费其语义，但只核对信封的存在、exactness 与跨信封一致性，不定义或逐字段验证上游 content schema。缺失、漂移或 receipt 不可验证时 fail closed，保留原 binding 并返回 typed input error。

Quest Goal、文献、历史、Evidence 与 guidance 分别保持自己的 exact binding；它们不能改写 `AcceptedQuestionBinding`。本 Run 新发现的材料不回写 frozen ContextPack。

## 语义操作

按稳定 `semantic_operation_id` 请求操作，由 Runtime 映射到已发现且已授权的工具；Skill 不绑定 MCP server、tool name 或 transport。

| 时机 | 语义操作 | 最小输入／结果 |
| --- | --- | --- |
| 进入及每次正式写入前 | `AdvancementEngine.observe_idea_stage_run`、`AgentRuntime.observe_idea_run_binding`、`AgentRuntime.verify_delivered_context_pack` | exact request／binding／pack ref+hash；返回 current typed observation。 |
| 检查 Run gate | `AgentRuntime.observe_run` | exact invocation closure；返回 pending／blocker／recovery observation。 |
| 保存 Outcome 内容 | `ResearchMemory.accept_idea_outcome_content`；未知时仅 `ResearchMemory.reconcile_idea_outcome_content` | submission identity、Outcome、review；返回 content ref 与原生 status／receipt。 |
| 提交领域 Outcome | `ResearchGraph.submit_idea_outcome`；未知时仅 `ResearchGraph.reconcile_idea_outcome` | 同一 submission、content ref／receipt、Outcome；返回 Outcome ref 与原生 status／receipt。 |
| 执行故障 | `AgentRuntime.report_execution_blocker` | exact request 与 blocker；返回 blocker receipt。 |
| 严格耗尽 | `AdvancementEngine.submit_exhaustion_proposal`；未知时仅 `AdvancementEngine.reconcile_exhaustion_proposal` | exact proposal identity／hash／closure；返回原生 status／receipt，不能自造 StageCommit。 |

## IdeaSubmission

```text
IdeaSubmission
  submission_identity
  stage_request_ref
  validated_runtime_binding_ref
  context_pack_ref
  context_pack_hash
  accepted_question_binding
  consumed_inputs[]
  discovered_inputs[]
  outcome: IdeaSet | NoViableCandidate
  outcome_hash
  review_record
  submission_lineage
```

`consumed_inputs[]` 只列实际影响 Outcome 的 exact binding。`discovered_inputs[]` 保存新材料的 ref、hash、provenance receipt 与 `accepted_evidence | research_observation | unresolved` 状态。同一 identity 只绑定一个 normalized payload；replay、feedback 与 reconciliation 规则见[候选与闭包契约](contract.md)。

## Accepted handoff

```text
AcceptedIdeaHandoff
  idea_outcome_ref: IdeaSetRef | NoViableCandidateRef
  rm_content_ref
  rm_content_accepted_receipt_ref
  rg_domain_accepted_receipt_ref
  run_execution_completed_receipt_ref
  stage_commit_ref
```

Plan 只接收完整 accepted handoff，不接收 draft、review finding、Session 状态、隐藏推理或未接纳 Submission。`ExhaustionProposalRef` 与 `rejected | stale | needs_input | outcome_unknown | technical_blocker` 都不是 accepted handoff。

## 验收

1. `AcceptedQuestionBinding` 不包含内容字段，schema ref 不被 Idea 固定为某个版本；调用者另行交付可读取且与 ref／hash 相符的 accepted content data。
2. Question／Quest／content ref、hash、schema ref 或 Owner receipt 缺失、使用可变 alias 或跨信封不一致时拒绝。
3. Submission、Owner accepted outcome 与 Stage handoff 是三个不同对象。
4. RM 与 RG receipt 始终分开；StageCommit 只作为外部接纳结果进入 handoff。
