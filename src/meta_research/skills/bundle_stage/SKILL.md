---
name: bundle-stage
description: 在已接纳 FormalPlan 内组织并启用 Target，滚动安排材料获取、整理、实验或其他研究，用真实接纳结果收口或说明语义修订理由。
---

# Bundle：组织实际研究

负责本轮工作范围、真实依赖与滚动安排；Target 根 Session 负责实施、检查、局部修订和结果交接。保持 Plan 承诺与 Idea 来源，在新结果出现时补充判断依据。检索或复用时读[研究导航](../references/research-navigation.md)，安排投入或委派时读[研究指导](../references/research-guidance.md)。

直接接收本轮同一 `cycle_focus` 原文及实际来源，用它理解 Plan 各份 Brief 的投入用意；按系统共同研究指导审视安排，有据调整写入既有 `StrategyUpdate.notes`，原文保留。重心的含义见[Cycle 工作](../references/cycle-work.md)，它不替代已接纳 Plan 或增加授权。

粒度、风险、正式工作与依赖查[Bundle 契约](references/contract.md)；调用与反馈查[Owner 操作](references/owner-operations.md)。Target 按运行时注入的 target-execution Skill、measurement_contract、result_schema 执行，Bundle 不重建另一套交接协议。


处理适用指导、主动材料或求助答复时，读[人类协作指导](../references/human-guidance.md)，结合实际影响安排本轮工作。
## 1. 读取与滚动安排

首轮理解完整已接纳 FormalPlan：全部回答义务、coverage 的依据与不足、每份 gap Brief、notes 和完整采用来源；结合稳定 Question、本轮重心、有效指导与授权选择值得现在投入的工作。Plan 原文与当前执行事实分别读取；目录摘要用于定位，缺少相关正文时先沿准确 reader 补齐，再冻结工作。已接纳 Plan 的 Brief 非空时进入 Bundle；Brief 为空时由 AE 按准确 Plan 的 disposition 及正式接纳事实跳过。未安排的真实 gap 与理由完整交给 Reasoning，不据跳过宣布责任已履行；具体边界见[Bundle 契约](references/contract.md#调用闭包与阶段范围)。

Plan 的 goal、characteristics、boundary_constraints、semantic_delta 保留原文。Bundle 据此派生完整 Brief 集的 cells 和 held-fixed 条件，说明每项工作的实际责任；这些派生字段不是要求 Plan 补填的缺项。冻结前核对完整 Brief，普通实现建议与明确科学承诺按[Bundle 契约](references/contract.md#从-plan-到正式工作)区分。

局部策略可随结果调整尚未提交的候选和顺序，不要求起步列尽未来路线。研究观察可指导选择，正式 coverage 只用已接纳 TargetCommit。同根滚动工作先给出 Cycle／Bundle 阶段／重心的简短定位，再接收本轮任务、新结果与变化，按[Owner 操作](references/owner-operations.md#同根后续输入)沿准确输入继续；需要回读时使用本次绑定的材料。

当前 Cycle 的相关工作材料沿实际获授入口核对，包括 Idea、Plan、已启用 Target 暂存材料和本工作已交付文件。宿主提供当前及前两轮实际可达的目录、工作归属与读写范围，按[材料与保管](../references/materials-and-custody.md#当前工作目录与回复材料)使用原生文件／shell；跨 Cycle 正式资产沿 RG／RM 准确入口读取。在途观察可指导安排，正式结果与冻结执行输入继续沿原交接流程。

`stage_context.read` 首次按 reader 传入 `offset=0`；后续仅在返回的 `next_offset` 为整数时将其作为下一页 offset。`next_offset=null` 即结束该正文的分页，按顺序拼接各页 text 后解析 JSON；`complete` 只表示当前响应是否含完整正文，尾页可仍为 false。遇 `semantic_input_schema_mismatch` 时查当前目录的参数 schema，修正具体字段后再调用，不原样重复失败请求。

需要复用方法或核验实施时，按[研究导航](../references/research-navigation.md)展开精确 Baseline、Variant、真实 Run 和适用评价，沿各自 reader 与分页读取；参数以当前工具 schema 为准，目录条目与正文分页分别处理。Plan 的完整采用来源同时包含科学支持与计划复用材料，以 Brief 的 reuse_material_refs 定位来源并按实际用途选择；派给 Target 前按[准确材料交接](references/contract.md#准确材料交接)落实关键情况说明与可读取内容。

需要回顾人类要求或协作历史时，经 RG 的 Human Interact 用途下 `human_request.read`（历史原文与处理）及 HC 的 `human_guidance.read`（当前指导） 返回的准确入口读取原文与实际处理，不把可见历史要求自动扩大为本轮约束。

仅 Bundle 经正式候选接纳与调度启用 Target；其他阶段、Target 和子智能体提供后续建议。Target 有独立可验目的、完成 cells、输入和实际依赖。一份 Brief 可由多个 Target 实现，一个 Target 也可履行多个 Brief 共有的责任；按实际责任和 cells 组织，不预设一对一或强行合并。获取、采集、清洗、整理可独立成 Target，也可与相关研究合并；粒度由研究需要、复用产物和真实依赖决定，湿实验、论证和辅助材料同样适用。

cells 表达应实施及报告的责任，允许负结果、失败原因和未解决判断，范围只含相关 obligations 与 Briefs。只有消费上游新结果才建立依赖，共享已接纳输入可并行。复用按实际需要核对数据、划分、预测、实现和协议，精确绑定资产版本。

按实际动作及 Quest 授权填 `risk_class`；已授权获取、整理、实施、分析和大型保存自主进行。需要人的专业知识、判断、权限、资源或行动时，按[主动发起与接续 HumanRequest](../references/human-guidance.md#主动发起与接续-humanrequest)请求协作；体积或耗时本身不构成额外授权门槛。

## 2. 据结果调整

首次 admission 仅处理未启动工作；已 claimed、running、recovering、finalizing 的工作沿既有 TargetRun 观察、wake 或 reconcile；已接纳 TargetCommit 用于收口。按证据、价值、资源与依赖选择 dispatch、wait 或 replan_required，说明理由。

给 Target 留出方法复用／新建、实际归属（含跨 Baseline）、实现和补充检查空间。保持 FormalPlan 的科学目的、支持要求、相对已有工作的增量和明确固定的边界，包括适用的 required Metric 与 held-fixed 比较条件；Characteristics 中的普通实现建议可有理由调整并在既有 notes 说明，不能借调整建议偷换科学比较。必须改变已承诺核心科学语义时交给 Reasoning 与后继 Cycle，同一 Cycle 不回 Plan。

## 3. 保存认识和可复用资源

用已接纳工作的真实结果更新覆盖，既可有测量，也可为无评价工作。负、零、不显著、不确定和缺测保持各自含义；准备审计只支持准备事实，不代替承诺中的实质研究。

`TargetPlan.notes` 和 `StrategyUpdate.notes` 保存研究判断；系统把最后一条非空且已接纳策略备注及 proposal ref／hash 交给 Reasoning，空值保留前次。后续备注保留仍有价值的线索及精确来源，更新认识、证据边界和取舍，避免被新一条备注覆盖后失去上下文。

已接纳 Target 的 `research_notes` 保存当时说明和最终发言。先读摘要，必要时沿 `research_notes_reader`／`research_memory.research_notes.read` 分页；精确正文用 `source=research_note_body`、`source_ref=version_ref`。上一 Question 的入口使用 `predecessor_research_notes_readers`，正文保留对应 `predecessor_ref`。说明有助理解，不替代完整合同与冻结输入。

Bundle 统一保留并筛选 Target 发现的建题线索。跨 Target 合并同一未知，先判断当前或已有 Question 能否承载；值得独立追踪的候选按对研究目标的重要性、已有依据和后续研究价值比较。交给 Reasoning 的建题推荐为零或一个，只选最值得推进的一项，说明来源、与已有题的区别和联系、独立追踪理由及可开展的研究。其余有价值线索留在 notes 中并明确为保留项，不逐 Target 提交、不作为待逐个建题的队列；没有合适候选时明确写本轮不推荐建题。这里的数量约束只针对新 Question 推荐，不限制 Target、Dataset、Environment 或 Literature 的合理粒度。

为保留最后一个 Target 后的汇总回合，安排和执行工作时保持 `strategy_complete=false`。已提出的 Target 全部接纳后，先对照完整 completion 责任检查是否仍需下一批；全部所需 cells 均有 current accepted TargetCommit 覆盖后，读取包括最后结果在内的研究说明、完成候选取舍和独立审阅，再用不新增 Target 的 `StrategyUpdate`（`candidates=[]`、`strategy_complete=true`）提交完整 notes 并封口；科学上的建题推荐写在 notes，不放进用于启用 Target 的 `candidates`。需要继续实施时按现有滚动策略提交实际 Target，保持策略未封口。技术阻塞或语义屏障仍按真实状态交接，不能为凑候选补造工作。

Target 正式接纳后，按[资源整理与正式登记](../references/materials-and-custody.md#资源整理与正式登记)读取候选、核验真实工作与精确内容，并整理值得复用的 Dataset／Environment／Literature。Target 过程中发现、取得或阅读的文献同样进入整理范围，保留外部文献来源、本次阅读判断与研究用途。

可委派明确范围的整理，根在本轮交接前查回必要记录、用途关系及适用的精确内容。完成条件：值得复用且当前可登记的候选已登记并读回；空候选或无保留价值时说明判断，不制造资源记录；未接纳、阻塞和未完成步骤按实际状态说明。普通 Literature 沿共享导航中的 register／reference／read 与统一原件 reader 完成；接口不可用、部分失败或未完成须保留具体步骤、已有来源与判断及其影响，不算无候选或整理完成。最后一个 Target 后 Bundle 若不再运行，由 Reasoning 在其综合交接前承接；暂存和未接纳产物按[交接与工作区收尾](../references/materials-and-custody.md#交接与工作区收尾)处理。现成资源的直接登记及适配关系沿[研究导航](../references/research-navigation.md)的 Environment 分支判断。

## 4. 复核与收口

重大候选与终态交接按研究需要委派原生独立子智能体审阅，根核对完整结果及必要原文并自主修订；最终交接的独立审阅不能用同根自查替代，不要求固定反馈数量或审查表单。

全部完成 cells 有 current accepted TargetCommit 覆盖时为 `realized`；需改变冻结语义时说明 `replan_required` 与剩余义务；真实阻塞、在途工作和未知效果保持实际状态。完成条件是本轮承诺已核对、结果与未决项已交接，Owner 接纳链可验证；执行结束、保存、TargetCommit 和科学结论分别成立。

需要人补足判断、资源或行动，或接续正式答复时，按[主动发起与接续 HumanRequest](../references/human-guidance.md#主动发起与接续-humanrequest)处理。查询原文与处理记录使用 RG 的 Human Interact 用途下 `human_request.read`（历史原文与处理）及 HC 的 `human_guidance.read`（当前指导） 返回的准确入口；实际依赖仅阻碍相应工作，其余 ready 工作继续安排。未知请求效果先沿原操作对账。
