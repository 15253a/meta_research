# Idea 输入与交接

## 当前科学输入

首轮及每次业务续轮／恢复的 user 正文，先按[本次阶段定位](../../references/cycle-work.md#本次阶段定位)直接说明请求绑定的 Cycle 身份、可用序号、当前 `Idea` 阶段与同一 `cycle_focus` 原文。定位来自本次工作绑定及准确来源；续接再追加实际变化，工具结果沿原生工具返回。

首轮直接给当前 Question 六字段全文一份，按[共同语义](../../references/cycle-work.md#新问题与后继-cycle)保留四项核心与两项按需可空补充；有直接前序时，给同一冻结前序 Reasoning 的完整 notes 与原结构化 scientific summary 各一份，保留真实来源及精确读取入口。notes 与 summary 各有独有科学信息，合并的是 current/prior 的同源重复表示。认识、失败、负结果、支持范围、限制、未决事项与复用条件随原交接保留。

本轮 `cycle_focus` 的同一原文及实际来源直接提供给 Idea，并随本轮各根接续；它说明本轮主要做什么，由 Idea 据此形成研究切入，再交 Plan 安排，不改写稳定 Question 或其现有六字段技术形状。首轮无 Reasoning 时沿准确创建/启动意图取得重心；确无起步意图则由首个根在既有 notes 简述并前传。正常后继的 focus 缺失如实交接，不能冒充首轮自行补造。共用语义与来源处理见[Cycle 工作](../../references/cycle-work.md)。

交接全文沿同一冻结前序来源取得；真正缺失、截断未补齐或不可访问时，如实说明缺什么及其影响，不能将预览或新摘要称为完整交接。其他历史按当前判断的相关性追溯。无直接前序时，保留这一事实，并按需要查当前 Question 和同 Quest 既有研究。

有 `creation_basis` 时，它指向正式 Question 关联的已有课题理解。用 `research_memory.stage_context.read` 的 `creation_understanding`、`creation_corrections` 和 `creation_sources` 分页读取。选中原件沿返回的 `research_memory.content.read` 精确版本入口继续读；未选中或未读原材料保留 `research_memory.creation_basis.read` 入口。无该绑定时不补造创建交接，材料缺失或 hash 变化如实呈现。外部旧成果保留其来源，不能当成本 Quest 新 Run。

## 判断当前工作所需的条件

沿本次提供的正文与准确入口理解以下内容；已有完整且仍有效的内容直接使用，缺少的部分按当前判断需要补足。

| 内容 | Idea 需要理解的事实 |
| --- | --- |
| Quest 与当前工作 | 当前完整目标、完成标准、有效持续条件、研究风格，以及影响构想的实际工作状态；目标版本与变化依据保留准确入口。重审方向时按[正式目标演化](../../references/cycle-work.md#正式-quest-目标演化)判断，区分已生效变化、仍待处理的影响和当时目标下形成的历史证据。 |
| 适用指导 | 当前适用集、用户最终确认的完整正文、范围、力度、持续条件和选中材料，以及已有处理、有效评估、分歧与待对齐事项。按[人类协作](../../references/human-guidance.md)读取与反馈，确认范围决定继承边界；新指导及时进入当前研究安排。 |
| 实际条件 | 本次授权、可用资源及实际运行状态；使用系统提供的当前事实，未知信息保持未知。投入与预算按[研究指导](../../references/research-guidance.md#研究预算与上下文)理解。 |

形成或比较候选前，当前 Quest 目标与有效条件尚未完整提供时，调用 `research_graph.quest_goal.read`（参数 `{}`），取得本操作绑定的完整目标、完成标准、持续条件与工作切面；已有完整且仍有效的正文直接使用。

新工作按 Idea 职责初始化；原工作未完成、提交被拒或正式求助回应可接续时，把本次任务与必要增量交回原根继续。原生 loop 已能取得结果或反馈时直接继续，压缩后仍按需要读取准确原件。适用指导的新增、修改和撤回及时交给相关根，由根结合已有安排调整研究；保留改变了什么、判断依据、未决影响与实际安排。普通指导不自动改变正式目标或解除执行限制。

新版工作中断或结果不明时，沿实际输入和既有执行记录核实与接续，初始输入记录保持原样；不把未知当未执行。原结果按本次输入解释，当前接纳与执行资格仍按实际状态判断，参见[修订与接纳](contract.md#修订与接纳)。优化后从头创建新版任务，不保留旧任务兼容路径。

## 材料发现与准确读取

沿[研究导航](../../references/research-navigation.md)发现相关积累，按[材料与保管](../../references/materials-and-custody.md)接续文件与工作。不同来源保持各自身份：

- RG/RM 中的正式研究内容沿精确版本与读取入口使用；相关历史、其他问题和暂放问题按研究需要追溯。
- 本次已获授的 Workspace 材料可用于接续脚本、草稿、中间认识和未完成的探索；其暂存、未接纳或未验证状态保持真实。当前及前两轮窗口内本根可达的路径、工作归属和读写范围由宿主列出，沿[当前工作目录](../../references/materials-and-custody.md#当前工作目录与回复材料)用原生文件／shell 按需读取；未给出的路径不表示没有研究历史。窗口外正式原件沿准确引用继续读取，回收由系统按[工作区收尾](../../references/materials-and-custody.md#交接与工作区收尾)处理。
- 创建 Quest、手动建题、主动指导与 HumanRequest 回复提供的原目录，沿本 Quest 当前授权的只读材料入口按需读取，不受临时 Workspace 三轮窗口限制；保留原提交、接收工作、实际已读部分与未读范围，撤权或离线按实际状态处理。材料入口与工作副本的用法见[工作资料处理](../../references/materials-and-custody.md#工作资料的当前处理与后继交接)。

已有准确正文、路径或 reader 时直接使用。来源位置不赋予正式证据身份；采用材料支持候选判断时，取得影响该判断的精确内容与必要限制。缺失、未读、不可访问、技术失败和科学不可行分别表达，交接可继续的研究与实际依赖。

已有准确引用、导航或 reader 时直接使用；需要发现正式资产时，用 `research_memory.stage_context.read` 的 `source=evidence_index` 在本次输入绑定的 Quest 范围内分页发现。当前该 reader 查询调用时的证据页，不能称作重放初始目录；使用返回的精确版本继续读取。读取目录由研究需要触发，不要求先取得固定首页才能开始构想。已有首页只作导航，来源总量、当前展示量与实际采用量分别理解；某页没列出、分页未完、授权范围内无记录、无权限与原件不可读各按实际返回说明，不能由一次未命中推断研究积累不存在。精确材料沿返回的版本与 reader 继续读取。

这个目录只覆盖本 Quest 的证据记录分页，没有相关性或自由搜索参数。其他研究积累按用途使用当前获授的 `research_graph.questions.page`、`research_graph.baselines.page`、`research_memory.literature.page` 及 Dataset／Environment 入口，沿返回的准确 reader 展开正文与关系；各入口的查询范围和分页以本次返回为准。

本次结果的 `evidence_boundary.accepted_evidence_refs` 与 `NoViableCandidate.candidate_families_considered[].evidence_refs` 合计最多 100 个不同引用，沿既有结果与接纳合同处理；这个范围针对实际采用／已考虑路线所列引用，不是目录必须展示或 Agent 必须读满的数量。索引服务发现，科学依据回到相关原文。

## 正式输入绑定

`IdeaStageInvocation` 带精确 `stage_request_ref`、验证过的运行绑定、ContextPack ref／hash、`AcceptedQuestionBinding` 与匹配正文，以及 Quest 目标、文献、历史、证据和当前指导的绑定。身份、hash、schema 和 receipt 在信封中保持一致；模型阅读视图保留判断与行动所需的准确身份，宿主保留权威绑定和恢复输入。

`AcceptedQuestionBinding` 含 Question／Quest／内容引用、内容 hash／schema、RM 内容 receipt 和 RG Question receipt；单独正文须与其精确匹配。Idea 消费科学含义，Question 的 schema 与生命周期由 Owner 管理。

文献修正绑定准确 predecessor 和 snapshot；RG 接纳及历史读回核验同 Quest 角色、RM receipt 与精确内容。不同来源的 reader、不同 Owner 的 receipt 按各自含义保留。

## 输出与交接

提交前按[主 Skill 的独立审阅与交接](../SKILL.md#2-独立审阅与交接)执行。IdeaSet 使 Plan 能判断候选价值、依据、适用条件、证据边界及关键未知。Plan 接到当前明确绑定的那一份完整已接纳 IdeaSet，包含该份中的全部候选；正式选择构想与本轮投入由 Plan 判断。同一 cycle_focus 原文与来源继续交给后继，有据调整的投入及理由写入既有 notes，不另造 focus 修改版。NoViableCandidate 则使 Reasoning 能理解本次探索范围、已考虑路线、不可行依据与可推翻条件，不能生成空 Plan，也不自行宣告 Quest 耗尽。两种结果均在既有科学字段和 notes 中保留影响后继工作的目标或指导变化、材料实际用途、未读范围、未决依赖及准确来源。

已有构想仍适用时，Idea 说明延续理由，不重造同义候选。后继入口／skip 的正式沿用由前驱 Reasoning 与既有接纳职责决定，不由 Idea 自行批准。

### 后继消费边界

Plan 的输入装配接收当前绑定的那一份完整已接纳 IdeaSet；skip 时沿用前驱 Reasoning 明确指定的准确集合，保留完整正文、原生产来源与本轮沿用依据。来源可以早于三轮 Workspace 窗口，沿正式准确引用读取；其他历史集合按需追溯。沿用构想不自动继承旧 Plan 的覆盖判断。这些是后继的输入职责，Idea 负责交出完整科学内容及其来路。

后继只消费完整已接纳交接：IdeaOutcome ref、RM 内容 ref／receipt、RG 领域 receipt、AR 执行完成 receipt 和 AE StageCommit ref。IdeaSet 进入 Plan，NoViableCandidate 进入 Reasoning；具体推进由 AE 按既定阶段条件安排。

草稿、审阅反馈、Session 状态、拒绝、未知结果和 ExhaustionProposal 都不等于已接纳交接。Submission、Owner 接纳结果与阶段交接是不同对象；RM／RG receipts 分别核验，StageCommit 仅来自 AE。
