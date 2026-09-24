---
name: bundle-stage
description: 在已接纳 FormalPlan 内组织并启用 Target，滚动安排材料获取、整理、实验或其他研究，用真实接纳结果收口或说明语义修订理由。
---

# Bundle：组织实际研究

负责本轮工作范围、真实依赖与滚动安排；Target 根 Session 负责实施、检查、局部修订和结果交接。保持 Plan 承诺与 Idea 来源，在新结果出现时补充判断依据。按根系统提示执行六入口、预算、人类输入和语言偏好，子智能体继承范围及语言。

粒度、风险、正式工作与依赖查[Bundle 契约](references/contract.md)；调用与反馈查[Owner 操作](references/owner-operations.md)。Target 按运行时注入的 target-execution Skill、measurement_contract、result_schema 执行，Bundle 不重建另一套交接协议。

## 1. 读取与滚动安排

读取已接纳 FormalPlan、gap Brief、输入索引、权威 frontier 与反馈，选择值得现在投入的工作。局部策略可随结果调整尚未提交的候选和顺序，不要求起步列尽未来路线。研究观察可指导选择，正式 coverage 只用已接纳 TargetCommit。

仅 Bundle 经正式候选接纳与调度启用 Target；其他阶段、Target 和子智能体提供后续建议。Target 有独立可验目的、完成 cells、输入和实际依赖。获取、采集、清洗、整理可独立成 Target，也可与相关研究合并；粒度由研究需要、复用产物和真实依赖决定，湿实验、论证和辅助材料同样适用。

cells 表达应实施及报告的责任，允许负结果、失败原因和未解决判断，范围只含相关 obligations 与 Briefs。只有消费上游新结果才建立依赖，共享已接纳输入可并行。复用按实际需要核对数据、划分、预测、实现和协议，精确绑定资产版本。

按实际动作及 Quest 授权填 `risk_class`；已授权获取、整理、实施、分析和大型保存自主进行，仅为具体外部权限、资源或人类动作提出 HumanRequest。体积或耗时本身不构成额外授权门槛。

## 2. 据结果调整

首次 admission 仅处理未启动工作；已 claimed、running、recovering、finalizing 的工作沿既有 TargetRun 观察、wake 或 reconcile；已接纳 TargetCommit 用于收口。按证据、价值、资源与依赖选择 dispatch、wait 或 replan_required，说明理由。

给 Target 留出方法复用／新建、实际归属（含跨 Baseline）、实现和补充检查空间。保持 FormalPlan 的 Goal、Characteristics、BoundaryConstraints、SemanticDelta、required Metric 和 held-fixed 条件；实质变化交给 Reasoning 与后继 Cycle，同一 Cycle 不回 Plan。

## 3. 保存认识和可复用资源

用已接纳工作的真实结果更新覆盖，既可有测量，也可为无评价工作。负、零、不显著、不确定和缺测保持各自含义；准备审计只支持准备事实，不代替承诺中的实质研究。

`TargetPlan.notes` 和 `StrategyUpdate.notes` 保存研究判断；系统把最后一条非空且已接纳策略备注及 proposal ref／hash 交给 Reasoning，空值保留前次。封口后未必还有写作回合，应及时写清认识、证据边界与未完成事项。

已接纳 Target 的 `research_notes` 保存当时说明和最终发言。先读摘要，必要时沿 `research_notes_reader`／`research_memory.research_notes.read` 分页；精确正文用 `source=research_note_body`、`source_ref=version_ref`。上一 Question 的入口使用 `predecessor_research_notes_readers`，正文保留对应 `predecessor_ref`。说明有助理解，不替代完整合同与冻结输入。

Target 正式接纳后，在同一整理核验环节读取 `dataset_candidates`、`environment_candidates` 和 completion manifest，沿 `artifact_path` 找真实 RM binding 并判断复用价值。Dataset 用 datasets.register／register_version／reference，真实派生用 derive；Environment 用 environments.register 记录含义、真实来源和相同原件 binding，再用 reference 关联当前 Question、原 Target／Run 和用途。六入口按复用目的组织，同一内容可供多个入口引用。

先发现或对账已有登记，复用身份和原件；可委派明确范围的整理，根独立查回记录、用途关系及适用的精确内容。最后一个 Target 后 Bundle 若不再运行，由 Reasoning 在其综合交接前承接；暂存和未接纳产物留工作区。已有设备、设施、持久目录、安装或服务按真实来源可直接登记，详见根系统提示的 Environment 语义。

## 4. 复核与收口

重大候选与终态交接按研究需要委派原生独立子智能体审阅，根核对完整结果及必要原文并自主修订；最终交接的独立审阅不能用同根自查替代，不要求固定反馈数量或审查表单。

全部完成 cells 有 current accepted TargetCommit 覆盖时为 `realized`；需改变冻结语义时说明 `replan_required` 与剩余义务；真实阻塞、在途工作和未知效果保持实际状态。完成条件是本轮承诺已核对、结果与未决项已交接，Owner 接纳链可验证；执行结束、保存、TargetCommit 和科学结论分别成立。
