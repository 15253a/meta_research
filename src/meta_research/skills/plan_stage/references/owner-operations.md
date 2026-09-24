# Plan 的 Owner 操作

以当前公开工具目录和 schema 为调用依据；本文件解释语义，不替系统生成身份、receipt 或控制事实。仅经公开接口核实，不读取 SQLite、spool、seal key 或控制文件绕过验证。

## 发现、读取和精确引用

ContextPack 提供创建时 evidence_catalog 及版本。`research_graph.plan_evidence.page` 通过 total、filter、next_offset 发现同 Quest 其他证据；`research_memory.plan_evidence.read` 读取精确 commit／version／hash／role 对应正文。已接纳后续条目可选用，Owner 在提交时重验引用与来源；后续发现不改写原 ContextPack。

TargetCommit 来源经现有验证接缝核查，包括有指标测量和无测量的观察、分析、负结果。科学含义由 Plan 说明，capability 与 provenance 如实记录，不能为可引用而补造测量。空目录不推导全库无证据，索引摘要不替代实际采用正文。

HumanInput、ScientificOutcome、AssetVersion、LiteratureSnapshot 按系统提示中的研究入口在当前 Quest 内发现，再沿返回 reader 读精确正文。额外来源采用：

```json
{
  "schema_ref": "meta-research/evidence-source-ref/v1",
  "evidence_ref": "精确来源ref",
  "source_kind": "HumanInput|ScientificOutcome|AssetVersion|LiteratureSnapshot",
  "source_ref": "同一精确来源ref"
}
```

`source_kind` 选实际单一类别，不写四类拼接值；coverage 的 evidence use 使用同一 ref。Target 证据使用工具返回的完整 EvidenceRef。`additional_evidence_bindings` 只提交本轮读过并实际采用的条目，最多 32 项；adapter 写入 `source_bindings.selected_evidence_catalog`，RG 重验真实接纳事实、同 Quest 范围和内容，并供后续读回。

Dataset／Environment 的登记与用途关系帮助发现；实际实施需要数字材料时，由 Bundle 绑定所需 AssetVersion；实体资源与现成服务按真实来源及已知使用条件安排，需适配的工作在相关 Brief 中说明。Environment 含义和使用知识供判断，本轮资源选择与预算读取当前运行条件，登记记录本身不证明资源可用或权限已授予。人类意见可供判断，不能替代执行授权或 HumanRequest 已满足事实。

## 职责与顺序

AE 管 request、epoch、StageCommit 和 skip；AR 管 Run、Attempt、根 Session、Fence、运行绑定、恢复和执行 receipt；RM 管内容保管、完整性、可用性和内容 receipt；RG 管 Question／Idea／FormalPlan、证据资格和领域 receipt。Plan Agent 负责义务、相关性、充分性、Brief 与修订，不是 State Owner。

系统依次验证精确输入及 current scope，核验已选来源，以独立操作身份保存 RM 内容、提交 RG 决策，接纳后形成 AR 执行完成事实，最后由 AE 提交 StageCommit。Agent 交付科学内容，不手工重建这些 receipts。

## 反馈与恢复

| 结果 | 处理 |
| --- | --- |
| `accepted` | 保存精确 ref／receipt，沿下一获授权步骤继续。 |
| `rejected` | 保存反馈及来源，在同一根 Session 实质修订后形成新提交。 |
| `stale` | 重验精确闭包及入选证据；不换成 latest。 |
| `needs_input` | 等待精确 HumanRequest 获 Owner satisfied 结果后继续。 |
| `outcome_unknown` | 对账原 operation，结果明确前不重放。 |
| `technical_blocker` | 说明真实范围，修复后从首个缺失步骤继续。 |
| `idempotency_conflict` | 保留同 key 的 payload 冲突并停止该写入。 |
| `already_sealed` | 消费原不可变接纳事实。 |

同一 operation／submission identity 只对应同一 payload 与调用闭包。重启或丢响应时按 Owner 已有事实续接，不重复创建已存在接纳记录。
