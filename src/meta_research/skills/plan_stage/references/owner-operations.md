# Plan Stage Owner 操作

这些是语义边界；运行时只调用安装产品公开的 Owner Interface，不读取 SQLite、spool、seal key 或控制文件来代替验证。

## 权限

- Advancement Engine 拥有 Plan StageRunRequest、foreground epoch、StageCommit 与 Bundle skip 验证。
- Agent Runtime 拥有 Run admission、Attempt、根 Session、Execution Fence、runtime binding、执行恢复与 execution receipt。
- Research Memory 拥有不可变 PlanDocument 内容、AssetVersion custody、完整性、可用性与内容 receipt。
- Research Graph 拥有 accepted Question/Idea/FormalPlan 身份、Evidence eligibility/currentness、ExperimentKey 关系与 domain receipt。
- Plan 主 Agent 拥有候选 AnswerContract、相关性与充分性判断、coverage/gap、ExperimentBrief、review disposition 和修订；它不是 State Owner。

## 调用顺序

1. 由 AE 验证当前 Plan request/epoch 和精确 accepted Question/IdeaSet handoff。
2. 由 AR 验证 runtime binding、ContextPack、根 Session 与 Fence，并记录每个 Attempt 的 durable provider operation。
3. 由 RM 和 RG 验证每个入选 EvidenceRef 的不可变内容、可用性、eligibility 与 currentness。
4. 由 RM 以 operation identity 接受 PlanDocument 内容，保存 content ref、payload hash 与 receipt checkpoint。
5. 由 RG 以新的 submission identity 接受或拒绝精确 PlanDocument binding，保存 FormalPlan ref 或 structured feedback receipt。
6. RG accepted 后由 AR 形成 execution-completed receipt；最后由 AE 验证 current request/epoch 与全部 receipts 并形成 Plan StageCommit。

## 原生结果

| Result | 处理 |
| --- | --- |
| `accepted` | 保存精确 ref 与 receipt，再进入下一个获授权 Owner。 |
| `rejected` | 保存 feedback 与 receipt，在同一根 Session 实质修订并使用新的 payload/submission identity。 |
| `stale` | 重新验证冻结闭包和入选 EvidenceRef。 |
| `needs_input` | 等待精确 HumanRequest 获 Owner satisfied disposition 后恢复。 |
| `outcome_unknown` | 协调原 operation identity；确认结果前不重放。 |
| `technical_blocker` | 保留已证明范围，修复后从首个缺失 receipt 恢复。 |
| `idempotency_conflict` | 停止写入并报告同一 key 的 payload 冲突。 |
| `already_sealed` | 返回既有 immutable accepted state。 |

同一 operation/submission identity 只绑定一个 payload 与 invocation closure。重启、丢 ACK 或局部成功时，查询 Owner 事实并从第一个缺失 checkpoint 继续；已存在的 RM/RG/AR/AE receipt 不重复创建。
