---
name: bundle-stage
description: 在已接纳 FormalPlan 内组织并启用 Target，滚动安排材料获取、整理、实验或其他研究，用真实接纳结果收口或说明语义修订理由。
---

# Bundle：组织实际研究

负责本轮工作范围、真实依赖与滚动安排；Target 根 Session 负责实施、检查、局部修订和结果交接。保持 Plan 承诺与 Idea 来源，在新结果出现时补充判断依据。按根系统提示执行六入口、预算、人类输入和语言偏好，子智能体继承范围及语言。

粒度、风险、正式工作与依赖查[Bundle 契约](references/contract.md)；调用与反馈查[Owner 操作](references/owner-operations.md)。Target 按运行时注入的 target-execution Skill、measurement_contract、result_schema 执行，Bundle 不重建另一套交接协议。

## 1. 读取与滚动安排

读取已接纳 FormalPlan、gap Brief、输入索引、权威 frontier 与反馈，选择值得现在投入的工作。局部策略可随结果调整尚未提交的候选和顺序，不要求起步列尽未来路线。研究观察可指导选择，正式 coverage 只用已接纳 TargetCommit。

本 Cycle 的 Idea、Plan、已启用 Target 暂存材料和本工作人类交付文件，用 `research_workspace.discover`／`read` 按根系统提示核对；在途观察可指导安排，正式结果与冻结执行输入继续沿原交接流程。

`stage_context.read` 首次按 reader 传入 `offset=0`；后续仅在返回的 `next_offset` 为整数时将其作为下一页 offset。`next_offset=null` 即结束该正文的分页，按顺序拼接各页 text 后解析 JSON；`complete` 只表示当前响应是否含完整正文，尾页可仍为 false。遇 `semantic_input_schema_mismatch` 时查当前目录的参数 schema，修正具体字段后再调用，不原样重复失败请求。

`research_graph.baselines.page`／`read` 的 `limit` 是条目数，默认 20，范围 1–100；`read` 只传精确 `baseline_ref` 即可读取方法合同，展开关联实体时再按返回的 `next_offset`／`next_evaluation_offset` 分页。各工具的分页单位独立，正文 reader 的字符上限不适用于 Baseline 列表。遇 `baseline_query_invalid` 时按当前 schema 修正参数，再继续读取。

`human_request.read` 先读取当前 Quest 索引，再直接使用返回的 request／response `reader` 参数读取原文；`request_ref` 保留完整修订后缀（如 `:r1`），`response_ref` 使用该 request 返回的绑定。遇 `research_help_request_unbound`／`research_help_response_unbound` 时回到索引核对这对引用，修正后再读。

仅 Bundle 经正式候选接纳与调度启用 Target；其他阶段、Target 和子智能体提供后续建议。Target 有独立可验目的、完成 cells、输入和实际依赖。获取、采集、清洗、整理可独立成 Target，也可与相关研究合并；粒度由研究需要、复用产物和真实依赖决定，湿实验、论证和辅助材料同样适用。

cells 表达应实施及报告的责任，允许负结果、失败原因和未解决判断，范围只含相关 obligations 与 Briefs。只有消费上游新结果才建立依赖，共享已接纳输入可并行。复用按实际需要核对数据、划分、预测、实现和协议，精确绑定资产版本。

按实际动作及 Quest 授权填 `risk_class`；已授权获取、整理、实施、分析和大型保存自主进行，仅为具体外部权限、资源或人类动作提出 HumanRequest。体积或耗时本身不构成额外授权门槛。

## 2. 据结果调整

首次 admission 仅处理未启动工作；已 claimed、running、recovering、finalizing 的工作沿既有 TargetRun 观察、wake 或 reconcile；已接纳 TargetCommit 用于收口。按证据、价值、资源与依赖选择 dispatch、wait 或 replan_required，说明理由。

给 Target 留出方法复用／新建、实际归属（含跨 Baseline）、实现和补充检查空间。保持 FormalPlan 的 Goal、Characteristics、BoundaryConstraints、SemanticDelta、required Metric 和 held-fixed 条件；实质变化交给 Reasoning 与后继 Cycle，同一 Cycle 不回 Plan。

## 3. 保存认识和可复用资源

用已接纳工作的真实结果更新覆盖，既可有测量，也可为无评价工作。负、零、不显著、不确定和缺测保持各自含义；准备审计只支持准备事实，不代替承诺中的实质研究。

`TargetPlan.notes` 和 `StrategyUpdate.notes` 保存研究判断；系统把最后一条非空且已接纳策略备注及 proposal ref／hash 交给 Reasoning，空值保留前次。后续备注保留仍有价值的线索及精确来源，更新认识、证据边界和取舍，避免被新一条备注覆盖后失去上下文。

已接纳 Target 的 `research_notes` 保存当时说明和最终发言。先读摘要，必要时沿 `research_notes_reader`／`research_memory.research_notes.read` 分页；精确正文用 `source=research_note_body`、`source_ref=version_ref`。上一 Question 的入口使用 `predecessor_research_notes_readers`，正文保留对应 `predecessor_ref`。说明有助理解，不替代完整合同与冻结输入。

Bundle 统一保留并筛选 Target 发现的建题线索。跨 Target 合并同一未知，先判断当前或已有 Question 能否承载；值得独立追踪的候选按对研究目标的重要性、已有依据和后续研究价值比较。交给 Reasoning 的建题推荐为零或一个，只选最值得推进的一项，说明来源、与已有题的区别和联系、独立追踪理由及可开展的研究。其余有价值线索留在 notes 中并明确为保留项，不逐 Target 提交、不作为待逐个建题的队列；没有合适候选时明确写本轮不推荐建题。这里的数量约束只针对新 Question 推荐，不限制 Target、Dataset 或 Environment 的合理粒度。

为保留最后一个 Target 后的汇总回合，安排和执行工作时保持 `strategy_complete=false`。全部 Target 已接纳后，读取包括最后结果在内的研究说明、完成候选取舍和独立审阅，再用不新增 Target 的 `StrategyUpdate`（`candidates=[]`、`strategy_complete=true`）提交完整 notes 并封口；科学上的建题推荐写在 notes，不放进用于启用 Target 的 `candidates`。需要继续实施时按现有滚动策略提交实际 Target，保持策略未封口。技术阻塞或语义屏障仍按真实状态交接，不能为凑候选补造工作。

Target 正式接纳后，在同一整理核验环节从 TargetCommit 上下文取精确 `target_ref`，调用 `research_graph.target_formal_results.read`，读取 `resource_candidates`。核对其 TargetCommit／manifest 身份，再看 `dataset_candidates`、`environment_candidates` 的 `artifact_path`、含义、精确 `asset_binding` 和 `producers`；候选 purpose 表示拟议用途，生产归属以 RG 当前已纠正的 subject 为准，原接纳归属及纠错记录保留依据。implementation 候选沿已核验 Run 的 `input_binding_ref`／implementation revision 关联实际工作。按内容判断复用价值，直接沿用完整原件 binding。Dataset 用 datasets.register／register_version／reference，真实派生用 derive；Environment 用 environments.register 记录含义、真实来源和相同原件 binding，再用 reference 关联当前 Question、原 Target／Run 和用途。六入口按复用目的组织，同一原件可供多种用途引用，交接不要求填满六入口。

需要核实实际内容时，对候选 binding 的精确 AssetVersion 调用 `research_memory.content.read`，将 `source_ref`、`version_ref` 都设为 `asset_binding.version_ref`。目录先省略 `entry_path` 并用 `offset=0, limit=1` 读取条目页，再选择所需正文。

先发现或对账已有登记，未知效果用原 `effect_id` reconcile，复用身份和原件；可委派明确范围的整理，根独立查回记录、用途关系及适用的精确内容。完成条件：值得复用且当前可登记的候选已登记并读回；空候选或无保留价值时说明判断，不制造资源记录；未接纳、阻塞和未完成步骤按实际状态说明。最后一个 Target 后 Bundle 若不再运行，由 Reasoning 在其综合交接前承接；暂存和未接纳产物留工作区。已有设备、设施、持久目录、安装或服务按真实来源可直接登记，详见根系统提示的 Environment 语义。

## 4. 复核与收口

重大候选与终态交接按研究需要委派原生独立子智能体审阅，根核对完整结果及必要原文并自主修订；最终交接的独立审阅不能用同根自查替代，不要求固定反馈数量或审查表单。

全部完成 cells 有 current accepted TargetCommit 覆盖时为 `realized`；需改变冻结语义时说明 `replan_required` 与剩余义务；真实阻塞、在途工作和未知效果保持实际状态。完成条件是本轮承诺已核对、结果与未决项已交接，Owner 接纳链可验证；执行结束、保存、TargetCommit 和科学结论分别成立。
