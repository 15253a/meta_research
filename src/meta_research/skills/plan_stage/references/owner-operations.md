# Plan 的工具使用与 Owner 交接

本文件说明 Plan 怎样使用材料、工具和正式结果。参数、分页与返回按需查[工具合同](tool-contracts.md)；科学输出见[Plan 合同](contract.md)。按本次实际授权和配置选择入口，未获授、不可达与无记录分别处理。

## 按研究问题选择入口

Plan 按研究用途选择获授权的 RG、RM、WF、HC 与外部 MCP，工作目录直接使用原生文件、shell 能力；子 Agent 沿宿主实际合同委派。Plan 根据研究目的安排阅读、分析、数据准备、脚本运行、复核和独立审阅；分工依据研究交付和正式承诺，不按脚本长短、耗时或是否运行代码划线。

| 眼前需要 | 直接可用的路径 |
| --- | --- |
| 已知路径、来源引用或 reader | 使用已知入口；缺少具体 schema 时按操作名取得合同。无需重新搜索来源或遍历工具目录。 |
| 接着当前或前两轮的脚本、草稿、中间认识工作 | 沿系统给出的实际目录、工作归属与读写范围，使用原生工具按需寻找和读取。 |
| 查方法、方案、执行过程、产物及评价 | RG Baseline；从实际命中的层级展开所需关系，再沿 reader 读原文。 |
| 理解问题的研究历程、过去 IdeaSet、判断及转向 | RG Question；近期已知材料也可直接读。 |
| 查文献、数据、环境、人类协作历史 | RG 的 Literature、Dataset、Environment、Human Interact 对应入口；具体操作可以由现有 RM/HC 实现。 |
| 获取本次交接、当前工作状态、恢复与提交反馈 | WF 的准确输入及原生工作续接能力。 |
| 当前求助、答复、新指导及其处理 | HC；已知请求／回复直接读取，历史请求、回复、主动材料及正式指导沿[工具合同](tool-contracts.md#当前工具入口)发现，按准确 reader 回顾。 |

六个 RG 入口按研究目的使用，不是每轮遍历清单。短期工作可使用原生文件能力及 HC、WF，近期研究也可以查 RG；材料年代不决定强制路径。完整研究导航见[六个研究视角](../../references/research-navigation.md)。

正常 Plan 的正式 Idea 输入是一份：本 Cycle 上游 Idea 产出并被接纳的完整 IdeaSet，保留其中全部 Idea。跳过 Idea 时沿用正式后继事实指定的准确 IdeaSet，即使其生产轮次更早。前驱 Reasoning 的本轮交接与 IdeaSet 生产来源分别保留。其他近期 IdeaSet 按需读取，更远历史由 Question 入口发现；不是把三轮或全历史的 IdeaSet 自动汇总进首轮。

本轮 `cycle_focus` 原文与现有来源随首轮输入直接交给 Plan；经过或跳过 Idea 都保留，正常后继缺失时核对正式交接装配，不自行补造。首轮起步、投入调整及焦点边界共用[本轮重心](../../references/cycle-work.md#本轮重心)，具体安排与必要理由留在既有 Plan notes。

## 发现、阅读与采用是三件事

目录摘要用于选择阅读方向。正文、过程、输入条件和评价支持当前的科学判断；准确绑定让后继能取得同一来源。依据关键判断需要展开相关内容，不要求全库查尽、全文读尽或提交读取证明。

已有足够依据决定某项支持是否适用、缺口是什么、下一步为什么值得做时，可以停止该项调查。仍会改变这些判断的明确线索、冲突或关键原件缺失，才推动继续查找、复核或求助。新任务在相关范围没有记录时，可以从当前 Question、IdeaSet 和已给材料开展工作；检索不命中本身不证明没有历史。

使用目录的返回范围、匹配方式和分页状态理解结果。未命中、没有记录、后页未完、权限不足和原件不可读分别处理，具体见[空结果与错误](tool-contracts.md#空结果与错误)。这些差别服务于研究决策，不增加 Agent 的逐项填表任务。

| 材料状态 | 研究用途与交接 |
| --- | --- |
| Workspace 工作副本 | 可读、可加工、可用于调查；记录相关路径与来路。文件存在或脚本运行成功不自动证明正式结果已接纳。 |
| 草稿和暂存认识 | 可影响本轮问题理解与工作安排；明确未验证之处，保留可继续的内容。临时结论不因进入笔记就变成 covered 的正式依据。 |
| 正式保管内容 | RM 的准确版本和 reader 提供正文；RG 表达它与问题、方法、执行、产物和评价的关系。内容保管不自动表示支持某个主张。 |
| 本轮正式采用来源 | 科学支持来源和 Brief 计划复用的材料统一放入 `selected_evidence_bindings`，保存 Plan 当时判断的来源；Owner 核验真实来源和权限。前者以 EvidenceUse 说明支持及边界，后者用 Brief.reuse_material_refs 表达复用意图；实际执行输入由 Bundle/Target 选择和绑定，不要求全量使用。 |

Plan 可以按既有授权使用公共材料保管、数据/环境登记及获取能力。需要把当前调查结果正式保管时，沿现有 RM/RG 接纳路径取得准确来源；不以排队、未命中对账、拒绝结果或私自制造 receipt、版本、执行记录代替接纳。Stage 自产材料、正式阶段正文与生产归属见[材料与保管](../../references/materials-and-custody.md#stage-自产材料正式阶段正文与独立保管归属)，公开 intake 现行受理范围与 reader 参数见[工具合同](tool-contracts.md#rm准确正文和材料能力)。

## 统一采用来源

`selected_evidence_bindings` 只包含本轮读过并用于判断或安排的准确来源，包括科学支持来源与 Brief 计划复用的材料。它保存本次 Plan 的采用历史，不是后继必须全部使用的清单；也不区分“首轮目录内”和“后续补充”，不预装候选证据。EvidenceUse 的 `evidence_ref` 引用集合中的同一项，支持主张与边界写在科学输出中。

Brief 可选的 `reuse_material_refs[]` 同样引用本集合的 `evidence_ref`，让 Bundle 直接取得计划复用的脚本、数据或其他材料，无需解析 notes 或重复发现。不是每个 Brief 都必须复用材料；普通复用意图允许后继选择、修复或替换，材料被引用也不自动支持 covered。明确固定的科学条件按[Plan 合同](contract.md#brief后继和最终交接)与现有语义修订职责处理，不把所有 characteristics 默认成实现硬锁。adapter 的 `evidence_reuse_set` 只由 EvidenceUse 派生。

adapter 将提交的完整 `selected_evidence_bindings` 原样放入正式 Plan 的 `source_bindings.selected_evidence_bindings`，保留 EvidenceUse 与 Brief 复用材料的所有条目。`source_bindings` 仍保留 Question、IdeaSet 等其他准确绑定；这里是同一采用集合的正式保存位置，不是另一份可独立修改的采用权威。Bundle 按 `Brief.reuse_material_refs` 中的 evidence_ref 在该列表定位准确 reader，再选择实际执行输入，不从 `evidence_reuse_set` 查找复用材料。后续实际选用内容及必要修复或替换按真实工作交接，不改写 Plan 的原采用记录，也不另加普遍偏离表。

以下展示正式采用形状；实际对象原样取 reader 返回的 selected_evidence_binding，示意占位值不能作为提交：

```json
{
  "selected_evidence_bindings": [
    {
      "evidence_ref": "演示占位：不可提交的准确来源引用",
      "source_kind": "AssetVersion",
      "source_ref": "演示占位：不可提交的准确来源引用",
      "source_binding": {
        "asset_ref": "演示占位：须取工具返回值",
        "version_ref": "演示占位：须取工具返回值",
        "content_hash": "演示占位：不可提交的内容hash",
        "manifest_hash": "演示占位：不可提交的manifest hash",
        "receipt": "演示占位：不可提交；实际使用Owner返回的完整receipt对象"
      },
      "reader": {
        "operation": "research_memory.content.read",
        "args": {"source_ref": "演示占位：须取工具返回值", "version_ref": "演示占位：须取工具返回值"}
      }
    }
  ]
}
```

`source_kind` 使用真实单一类别：`TargetCommit`、`HumanInput`、`ScientificOutcome`、`AssetVersion` 或 `LiteratureSnapshot`。`source_binding` 保留该类型现有的准确来源身份和接纳来路；类型之间不强求同一内部结构。Target 来源保留真实 commit、版本、内容和能力边界；没有数值测量的观察、分析及负结果仍按实际接纳事实使用，不补造指标。工具负责返回绑定，Agent 选择并解释用途，不手拼 authority。

AssetVersion 示例展示 asset_ref、version_ref、content_hash、manifest_hash、receipt；实际 `source_binding` 原样保留该 Owner 返回的完整结构，这些示意字段不是新增 allowlist 或校验规则。准确值来自工具，不是 Agent 自述或 adapter 新造的接纳事实。真实 receipt 为 Owner 返回对象，示例中的占位字符串不是真实身份。非 Target 的通用来源允许 `evidence_ref=source_ref`，二者作用分别为采用集合引用和准确来源引用，不为区分字段而制造第二个身份。

LiteratureSnapshot 的绑定引用已接纳快照；论文 DOI、URL、citation key 及页/节定位解释读了哪篇、支持什么，不替代快照身份。同一快照可支持多个独立 evidence use。五类采用来源复用各自现有发现与读取位置：目录命中若未直接带出完整采用绑定，沿其提供的准确 reader 读取后取得，不要求每个发现页和每一层返回都复制完整投影，也不从标题猜引用。

Reader 是准确访问入口，不是新的证据身份。已知来源可直接读取；调用参数与展示信息的区分见[Reader 工具合同](tool-contracts.md#rm准确正文和材料能力)。分页大小限制一次读取，不自行变成本轮最多能采用多少来源的科学限制。实现如有真实容量限制，应明确返回并支持合理拆分，不静默丢失来源。

## Workspace 的三轮滑窗

宿主按正式 Cycle 序列提供当前及前两轮窗口内本根可见的目录、工作归属与读写范围。Plan 用原生文件／shell 读取已列出的本轮上游与历史工作，只编辑自己的 `read_write` 工作目录；未列出的路径不能推定可读或不存在。准确范围及晚到原根、未提供路径的处理见[当前工作目录](../../references/materials-and-custody.md#当前工作目录与回复材料)。

出窗清理与 Session 回收由系统结合工作身份及真实依赖执行，`linked_local` 原件、活动与恢复依赖、长期根和外部只读原目录沿[收尾约定](../../references/materials-and-custody.md#交接与工作区收尾)分别保护。Plan 保管需要接续的内容并交接真实未决事项，不代系统遍历或删除历史目录；文件时间或已归档状态不代替释放资格。

## 覆盖与安排的派生及阶段交接

覆盖、Brief 关联约束与未安排 gap 的派生按[Plan 合同](contract.md#brief后继和最终交接)处理；未安排部分的实际不足、本轮不安排理由、依据及限制随完整 Plan 交接。

`bundle_disposition` 按同一合同从 Brief 是否为空派生。正式 Plan 接纳后，AE 依其准确内容、由空 Brief 派生的 disposition 及既有正式接纳事实形成 Bundle `StageCommit(Skipped)`，不建立 Bundle Run 或 Target。Plan 的完成只确认规划阶段履责，跳过不确认所有 obligations 已履行。Reasoning 接收完整精确 Plan 及其中的实际缺口和理由；skip 记录引用该 Plan，不复制第二份 coverage／gap 台账。

## 正式交接、反馈和原工作续接

Plan 负责义务、相关性、充分性、工作安排及不安排理由、Brief 和修订。RG 按结构、来源、身份、权限和正式事实接纳研究决策与来源关系，RM 保管内容，AE 推进阶段，AR 维护运行和根 Session，HC 保存协作原记录与处理。科学价值和理由是否充分由根判断；Owner 不科学审批，字段非空不构成理由充分的证明，独立审阅也不是批准。工具分组不要求 Agent 扮演这些 Owner。

Plan 交付科学内容后，由平台封装精确输入核验、RM 保管、RG 接纳、运行完成及 AE StageCommit。提交结果返回已接纳引用或有内容的反馈；Agent 无需编排 receipts、spool、seal 或内部状态。未知效果沿原操作对账，结果明确前不重放。正式结果以真实接纳事实为准，不以原生工具成功代替。

| 返回语义 | Plan 的下一步 |
| --- | --- |
| `accepted` / `already_sealed` | 消费准确的已接纳内容与引用，交接给获授权的后续阶段。 |
| `rejected` | 理解反馈并在原根实质修订；新提交表达新内容。 |
| `stale` | 区分旧操作对账与已变化输入；系统授权的新逻辑操作使用自己的准确切面，并接续已有认识。 |
| `needs_input` | 无可独立继续的工作且成功发起 HC 请求后，结束当前执行、交回系统；条件正式解除后由系统 resume 原根。回复已到达不等于条件已满足。 |
| `outcome_unknown` | 对账原操作，保持原 payload；未知效果不重新创建接纳或请求。 |
| `technical_blocker` | 保留相关工作，继续独立可做部分，修复或求助后接续；不据此判成无历史或科学不可行。 |
| `idempotency_conflict` | 停止该冲突写入，核对原操作与内容。 |

独立审阅、新指导、求助答复和必要恢复回到原根工作。续接只补充变化、准确材料、当前任务与反馈；重审真正受影响的义务、来源、coverage 和 Brief，保留仍适用的认识。正常独立审阅由根组织，不重新创建 Plan 来重走首轮调查。

材料、授权或答复仍是当前规划判断的必要条件且无可独立继续的工作时，HC 成功 open 后“结束本次执行”是停止当前推进、交回系统等待，不是完成 Plan、关闭根 Session 或生成 StageCommit。已完成的调查、草稿与认识留在原 Session／Workspace；此时不提交不完整 FormalPlan，不另造恢复包，不自行轮询。系统核验正式答复并解除条件后 resume 同一根；其他不受该依赖阻塞的独立根可继续工作。有依据的本轮不安排判断不代替已有 HumanRequest、在途承诺和有效人类条件的正式处理。

在途指导新增、修改或撤回应通过成熟原生输入能力及时送入适用原根；HC 保留原文、范围、力度、处理和实际影响。原根结合当前授权解释如何调整；接口收件、Agent 已理解和研究安排已改变是不同事实。求助回复的文件沿系统给出的实际可访问路径，用原生文件能力读取；协作历史按[人类协作与指导入口](tool-contracts.md#当前工具入口)发现与准确读回。

涉及 Quest 目标或有效条件变化时，使用 `research_graph.quest_goal.read` 及[正式目标演化流程](../../references/cycle-work.md#正式-quest-目标演化)。当前草稿随新认识修订；已接纳 FormalPlan 的核心语义不被静默改写，确需改变已承诺工作时按现有协调与 Reasoning 后继职责处理。系统维持恢复所需的指令、工具、权限和准确输入；科学判断继续由 Agent 完成。
