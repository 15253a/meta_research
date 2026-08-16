# Plan Stage Owner 操作

在证据查询、正式写入、结果协调、阻塞上报或耗尽提案前使用本文件。以下名称表达语义调用，不冻结生产签名或传输方式。

## 权限

- Advancement Engine：拥有 Plan StageRunRequest 当前性、Foreground Epoch、Bundle skip 验证和 StageCommit。
- Agent Runtime：拥有 Run admission、Execution Attempt、根 Session、Execution Fence、Capability／Resource Binding、恢复、执行阻塞和执行回执。
- Research Memory：拥有不可变 PlanDocument 内容、AssetVersion、保管、完整性、可用性和内容回执。
- Research Graph：拥有已接受 Question／Idea／FormalPlan 的身份与关系、Evidence eligibility／domain currentness、FormalPlan 内的 ExperimentKeys 和 Plan 接受回执。
- Plan 主 Agent：在当前根 Session 内拥有候选 obligations、Idea relevance、证据相关性／充分性、gap、ExperimentBrief 语义、审阅处置和修订；它不是 State Owner。

主 Agent 可以按任务需要调用一个或多个子智能体，以串行或并行方式处理冻结输入内的检索、证据核验、草案质询和辅助分析。每个子智能体只接收完成其分支所需的最小冻结输入，并把 candidate、finding 和 provenance 返回主 Agent。主 Agent 保留搜索边界变更、最终充分性判断、根 Execution Fence 使用和正式写入权；Owner 继续签发所有正式回执。

## 已解析的语义调用

```text
observe_plan_stage_run(...)
  TODO-IMPL(advancement_engine.observe_plan_stage_run; source=#58)

observe_plan_run_binding(...)
  TODO-IMPL(agent_runtime.observe_plan_run_binding; source=#71)

verify_delivered_context_pack(...)
  TODO-IMPL(agent_runtime.verify_delivered_context_pack; source=#71)

query_evidence(open | follow | refresh, ...)
  TODO-IMPL(plan_interface.query_evidence; source=#62)

verify_evidence_refs(...)
  TODO-IMPL(research_graph.verify_evidence_refs; source=#57)
  TODO-IMPL(research_memory.verify_evidence_assets; source=#66)

submit_plan_content(...)
  TODO-IMPL(research_memory.accept_plan_content; source=#62)

reconcile_plan_content(...)
  TODO-IMPL(research_memory.reconcile_plan_content; source=#62)

submit_formal_plan(...)
  TODO-IMPL(research_graph.submit_formal_plan; source=#62)

get_submission(...)
  TODO-IMPL(plan_interface.get_submission; source=#62)

report_execution_blocker(...)
  TODO-IMPL(agent_runtime.report_execution_blocker; source=#90)

submit_exhaustion_proposal(...)
  TODO-IMPL(advancement_engine.submit_exhaustion_proposal; source=#90)

reconcile_exhaustion_proposal(...)
  TODO-IMPL(advancement_engine.reconcile_exhaustion_proposal; source=#90)
```

候选 AnswerContract 由本 Skill 的 Plan 语义解析，不由上游 Owner 操作创建。`submit_formal_plan(...)` 携带精确候选，交给 Research Graph 接受。

`TODO-CONTRACT`: none.

## 原生结果

| Result | 主 Agent 动作 |
| --- | --- |
| `accepted` | 保存精确 Owner ref 与回执，再进入下一个获授权的 Owner。 |
| `rejected` | 保存反馈与回执，在同一当前根 Session 修订，并使用新的 payload identity。 |
| `stale` | 重新验证冻结闭包与入选证据，再继续原语义操作。 |
| `needs_input` | 绑定精确 HumanRequest，等待其 Owner 记录 satisfied disposition 后恢复。 |
| `outcome_unknown` | 协调原 operation identity，确认原写入结果后再决定后续动作。 |
| `technical_blocker` | 按已证明范围上报并关闭该路径；修复或等待后重新验证。 |
| `idempotency_conflict` | 停止当前写入，报告同一 key 对应不同 payload。 |
| `already_sealed` | 返回既有 FormalPlan 状态。 |

RM 接受与 RG 接受是两个事实。RM 接受而 RG 拒绝时，保留不可变 PlanDocument 作为未绑定资产；在当前 Session 修订草案，并为变化后的内容提交新 AssetVersion。响应丢失时，先协调原操作身份。

## Fixture 纪律

参考脚本可以用显式 `fixture:` ref 和确定性 fake port 实现上述操作。fake 结果只证明路由行为；生产当前性、回执、接受、执行、跳过和 Stage 推进仍需真实 Owner 事实。
