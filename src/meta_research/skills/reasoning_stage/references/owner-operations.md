# Reasoning Stage Owner 操作

Reasoning root 只获得三项高层、closed、current AR-scope 的 Semantic MCP read：

1. `advancement_engine.reasoning_stage_run.observe`：核验 current StageRunRequest、epoch 与 AE closure。
2. `research_memory.reasoning_evidence.read`：读取 exact frozen evidence content/role closure，不接受新内容；Plan reuse 只呈现已经由 RG/TargetCommitEvidenceAuthority public verifier 重建并冻结的 MetricResult leaf。
3. `research_graph.reasoning_context.read`：读取 exact accepted Question/Quest/Goal 与 domain context，不改变图。

本 Skill catalog 不暴露 Agent-callable Owner effect。若未来单独版本化 effect，catalog admission 必须同时携带 matching reconciliation operation，并在调用前重新观察 currentness；缺一即 fail closed。

## 权限边界

- Advancement Engine：StageRunRequest、Foreground epoch、StageCommit、successor Cycle 与 Quest ending transition。
- Agent Runtime：Run/Attempt/root Session/Fence、durable provider operation、execution receipt 与恢复。
- Research Memory：不可变 ScientificOutcome/transition content custody 与 content receipt。
- Research Graph：scientific/domain acceptance、QuestionAnchor、Goal/completion semantics 与 domain receipt。
- Human Collaboration：Web preview、明确用户 completion confirmation 与 HumanRequest；response 不自动等于 satisfied/resumed。
- Reasoning Agent：证据受限的 scientific judgment、唯一 transition 候选与 child-review disposition；不是 State Owner。

## 写入与恢复

Provider 正常完成后，daemon 才依次经公开 Owner Interface 保存 AR execution fact、RM immutable content、RG domain decision，并最终由 AE 在 current request/epoch 与全部 receipt 可验证时形成 Reasoning StageCommit。每个 boundary 使用独立 idempotency identity；response-lost 先查询／reconcile 原 identity。Skill 不读数据库、spool、seal key 或私有状态机来代替这些验证。

CandidateCompletion 还必须经历 current Web Preview、用户明确确认、RG Goal/completion acceptance 与 AE ending transition；任一拒绝、stale、未知或无响应均不得结束 Quest。
