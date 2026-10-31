# Plan 的 Owner 操作

以当前公开工具目录和 schema 为调用依据；本文件解释语义，不替系统生成身份、receipt 或控制事实。仅经公开接口核实，不读取 SQLite、spool、seal key 或控制文件绕过验证。

## 发现、读取和精确引用

人类或 Agent 取得的工作资料与后继已保管材料，按[共享资料处理合同](../../references/materials-and-custody.md#工作资料的当前处理与后继交接)发现、精确读取、处理及反馈。材料采用关系服务于本轮安排判断，正式执行输入继续由 Bundle 绑定。

按实际研究需要调用 `research_graph.plan_evidence.page`，通过 total、filter、next_offset 发现同 Quest 证据；`research_memory.plan_evidence.read` 读取精确 commit／version／hash／role 对应正文并返回 `selected_evidence_binding`。ContextPack 不预查候选目录，后续发现不改写原冻结请求。目录页长不限制本轮采用来源范围。

TargetCommit 来源经现有验证接缝核查，包括有指标测量和无测量的观察、分析、负结果。科学含义由 Plan 说明，capability 与 provenance 如实记录，不能为可引用而补造测量。空目录不推导全库无证据，索引摘要不替代实际采用正文。

HumanInput、ScientificOutcome、AssetVersion、LiteratureSnapshot 按系统提示中的研究入口在当前 Quest 内发现，再沿返回 reader 读精确正文；目录未带完整采用绑定时，从准确 reader 返回的 `selected_evidence_binding` 取得。完整采用集合 `selected_evidence_bindings` 中每项形状为：

```json
{
  "evidence_ref": "精确来源ref",
  "source_kind": "AssetVersion",
  "source_ref": "准确来源ref",
  "source_binding": {"说明": "原样使用实际Owner返回的完整绑定，此示意对象不可提交"},
  "reader": {
    "operation": "research_memory.content.read",
    "args": {"source_ref": "工具返回的配对引用", "version_ref": "工具返回的准确版本"}
  }
}
```

`source_kind` 使用真实单一类别：TargetCommit、HumanInput、ScientificOutcome、AssetVersion 或 LiteratureSnapshot；source_binding 保留该类型现有准确身份和接纳来路，reader 保留工具返回的准确操作及参数，不手拼 receipt 或引用。覆盖的 EvidenceUse 与可选 Brief.reuse_material_refs 都以 evidence_ref 引用同一集合。adapter 将整个 `selected_evidence_bindings` 原样写入 `source_bindings.selected_evidence_bindings`；RG 核验真实接纳事实、当前权限、同 Quest 范围和内容，后续按准确身份读取。只供实施的材料不必伪装成 EvidenceUse，后继按实际需要选择或修复材料时保留 Plan 原采用历史。

采用文献时，`LiteratureSnapshot` 的 `source_ref` 和 `evidence_ref` 都复制已接纳文献修订返回的 `literature_snapshot_ref`（`literature_snapshot_...`）。DOI、URL 和 `citation_key` 定位快照中的具体论文，写在 `supported_claim`、`support_boundary` 或 notes 中；它们不是快照身份。同一快照的多篇论文共用一个来源 binding，各 evidence use 分别说明所读论文、主张及适用边界。提交前逐项对照工具返回的来源类型和精确 ref。

Dataset／Environment 的登记与用途关系帮助发现；实际实施需要数字材料时，由 Bundle 绑定所需 AssetVersion；实体资源与现成服务按真实来源及已知使用条件安排，需适配的工作在相关 Brief 中说明。Environment 含义和使用知识供判断，本轮资源选择与预算读取当前运行条件，登记记录本身不证明资源可用或权限已授予。人类意见可供判断，不能替代执行授权或 HumanRequest 已满足事实。

## 职责与顺序

AE 管 request、epoch、StageCommit 和 skip；AR 管 Run、Attempt、根 Session、Fence、运行绑定、恢复和执行 receipt；RM 管内容保管、完整性、可用性和内容 receipt；RG 管 Question／Idea／FormalPlan、证据资格和领域 receipt。Plan Agent 负责义务、相关性、充分性、Brief 与修订，不是 State Owner。

系统依次验证精确输入及 current scope，核验已选来源，以独立操作身份保存 RM 内容、提交 RG 决策，接纳后形成 AR 执行完成事实，最后由 AE 提交 StageCommit。Agent 交付科学内容，不手工重建这些 receipts。

证据或正式人类指导要求改变 Quest 方向时，用 `research_graph.quest_goal.read` 核对本逻辑操作的冻结切面，按共享指导合同作完整判断，再用 `research_graph.quest_goal.evolve` 提交；未知效果用相同 effect 的 `.reconcile`。新入场工作使切面过期时，新逻辑根操作才会得到新切面。

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
