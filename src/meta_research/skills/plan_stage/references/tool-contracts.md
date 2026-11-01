# Plan 的工具合同

选择操作、解释分页／错误或准备采用来源时读取本文。实际参数以本次已注册工具的 schema 为准；科学取舍与采用语义见[Owner 操作](owner-operations.md)。同一来源的准确 reader 可以直接调用，不要求先遍历目录。

## 当前工具入口

按需用 `tools.catalog.page` 发现本根当前可用的操作，以 `tools.contracts.read(operation_id)` 读取准确 schema、返回及错误说明；分页和授权变化的处理见[当前获授工具与准确入口](../../references/research-navigation.md#当前获授工具与准确入口)。`tools/list` 仍提供本根当前可用的操作与 schema；已知 reader 可直接调用。RG、RM、RW、WF、HC 是研究用途分组，不构成新的 Owner。下面保留研究用途与读取语义，参数范围以当前合同为准。

| 需要 | 当前入口和边界 |
| --- | --- |
| 当前 Question、IdeaSet、前驱与 focus | 首包直接给准确完整正文；必要恢复使用 `research_memory.stage_context.read` 的冻结输入及返回的精确 reader。输入来源与原始 IdeaSet 生产轮次分别保留。 |
| 同 Quest 的候选证据 | `research_graph.plan_evidence.page` 按页提供候选身份和来源；没有首次必读页，页长不是科学采用上限。 |
| 选中的 Target 证据正文 | `research_memory.plan_evidence.read` 的准确身份取工具返回值，按返回的 next_offset 继续。返回的 `selected_evidence_binding` 原样用于采用集合。该专用 reader 的正文偏移按其 `total_characters` 字符范围处理，不套用通用 reader 的 UTF-8 字节规则。 |
| 问题及已有综合 | `research_graph.questions.page`、`research_graph.question_history.read`、`research_graph.question_relations.read`，按返回的范围、分页和准确正文入口展开。问题历史当前列已接纳 Reasoning，完整 Question／Cycle 历史阶段发现由 #267 承接。 |
| 方法、方案、运行及评价 | `research_graph.baselines.page`、`research_graph.baselines.read`、`research_graph.formal_results.read`、`research_graph.target_formal_results.read`；当前匹配范围与展开关系以真实返回为准，执行及产物研究文字的增强发现、独立关系分页由 #267 承接。 |
| 文献 | `research_memory.literature.page` 与返回的准确修订 reader。当前目录依据 Question 文献修订，不代表全部历史全文或任意取得材料已入索引；空 items 仍检查 next_offset。已知旧修订按精确 reader 读取。 |
| Dataset／Environment | `research_graph.datasets.page`／`.read` 与 `research_graph.environments.page`／`.read`，按各自 schema 选择一种查询身份或过滤方式；登记、版本、用途和派生使用本次获授的对应写入及 reconcile 操作。数字原件沿 asset binding 的准确 reader 取得。 |
| 人类协作与指导 | 当前 `human_request.read`、`human_guidance.read`、`human_guidance.feedback`；请求／回复、主动材料和实际处理分别读回。RG Human Interact 的统一检索与关系分页由 #267 承接，不能以没有该目录推断不存在协作历史。 |

## RM：准确正文和材料能力

`research_memory.content.read` 需要配对的 `source_ref` 与 `version_ref`，可按 schema 提供 entry_path、offset、limit。沿工具返回的 reader 原样调用；版本值可能是准确内容身份或该来源规定的内容 hash，不能用 latest、文件路径、DOI 或自算 hash 替换。通用正文通常按 UTF-8 字节分页；按该返回的 offset_unit、encoding、next_offset 解释，不以字符数或列表记录数代替。`complete` 表示单次返回是否包含完整对象，分段末页以 next_offset 结束判断。

Plan 接受 TargetCommit、HumanInput、ScientificOutcome、AssetVersion 和 LiteratureSnapshot 五类采用来源。reader 提供的 `selected_evidence_binding` 包含 evidence_ref、source_kind、source_ref、source_binding、reader，原样保留。EvidenceUse 说明支持主张与边界；仅供 Brief 复用的材料也在完整集合中保存。普通人类意见按真实内容解释，不自动成为测量、授权或完成确认。

`research_memory.assets.intake` 当前公开受理 text 或固定 target_workspace_note 来源，现有受理范围为 managed。任意获授权本地文件／目录及 linked_local 公开保管扩展由 #266 接通；不从底层枚举或内部方法猜测接口可用。排队、未命中对账及拒绝结果不是已接纳资产；成功结果和配对 reader 以真实 Owner 返回为准。

调用 `research_memory.research_notes.read` 时仅传工具 schema 参数；`summary_only` 是阅读视图说明，不是 reader 参数。正式原件的当前状态、更正及可用性遵循实际返回，不能因保留历史身份而忽略当前执行资格。

## WF：工作交接、提交和连续性

`research_memory.stage_context.read(context_pack_ref, source, source_ref?, path, offset, limit)` 读取冻结输入与获授历史。`path` 是 JSON 路径数组，`[]` 读取根；offset 是序列化正文 UTF-8 字节，按返回继续。`source=predecessor_closure` 读取正式前驱，`source=scientific_outcome` 读取准确综合；支持历史目录的模式另用 index_offset，与正文 offset 分开。必要首轮科学正文已直接给出，reader 用于深入与恢复，不用它替代首次完整输入。

每次业务输入保留同一请求绑定的 Cycle／Plan／focus 定位；已有科学输入不从 latest 补造。原生同根继续研究、独立审阅、聚合和修订，收到正式反馈后只重审实际受影响工作。Harness 维持适用指令、工具、权限和 schema；引用的资源读不到时报告准确文件与影响，不退回另一版文案。

## 原生工作文件与待接通范围

普通文件沿本次实际给出的目录和授权使用原生工具。当前路径绑定、HumanRequest 材料递交与 inbox 的现有职责继续使用；当前及前两轮的完整路径、旧普通文件包装退出和窗口外条件回收由 #268 接通。不要根据目标生命周期假定未提供目录已可访问，或代系统清理历史。正式保管原件沿 RM 精确版本，研究关系沿 RG，协作按 HC，工作接续按实际 WF 入口。

## 求助、回复与有效指导

`human_request.open` 的参数按本次 schema 提供具体问题、已尝试内容及所需答复；发起后无可独立继续工作时交回系统等待。未知效果以原 effect_id 调 `human_request.open.reconcile`，已知 request_ref／response_ref 时使用 `human_request.read` 读原回复及实际状态。请求列表 cursor、主动材料 input_offset 与正文 offset 各沿返回继续；回复抵达不等于授权或条件已满足。

指导正文、范围、力度和处理结果沿 `human_guidance.read`／`.feedback` 使用；已有完整读取 receipt 的实际限制保持，不把它当作科学充分性的证明。目标或完成标准确需改变时按[正式目标演化](../../references/cycle-work.md#正式-quest-目标演化)处理，不静默改写冻结 Plan 输入或已接纳承诺。

## 空结果与错误

一次未命中仅说明本次范围及匹配方式下没有返回项；有 next_offset／cursor 时仍有后页。授权范围无记录、过滤未命中、分页未完、缺权限、原件不可读、服务故障分别保留其真实含义。实际返回的 isError、code、details 和 Owner status 决定错误或状态；未注册或未获授操作不是合法空页。

accepted／already_sealed 表示准确接纳结果；rejected 需要原根按反馈修订；stale 保留旧操作及变化事实；outcome_unknown 对账原操作；technical_blocker 保留具体阻塞和可独立继续工作。可恢复技术故障不成为科学不可行结论。参数错误依 schema 修正，idempotency_conflict 先核对原操作及内容，不换身份盲目重试。
