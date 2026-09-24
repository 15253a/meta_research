# Bundle 契约

## 调用闭包与阶段范围

`BundleStageRunRequest` 冻结 stage request、Cycle、epoch、Bundle Run、根 Fence、ContextPack ref／hash、已接纳 FormalPlan ref／内容 hash／receipt、EvidenceReuseSet、GapSet、全部 gap Brief，以及 Quest 授权、Harness 和资源边界。消费 current FormalPlan 的精确内容，稳定 ref 本身不替代绑定；草稿或漂移的 latest 不能启用 Target。

没有 gap Brief 时由 AE 验证正式 Plan 及 receipts 并形成 `StageCommit(Skipped)`，不建立 Bundle Run 或 Target。冻结核心承诺的实质改变通过 `replan_required` 交给 Reasoning 与后继 Cycle。

## 策略、粒度与风险

Bundle 在 Session 内维护可修改的滚动策略：未解 Brief／cells、已接纳上游闭包、候选和真实依赖、方法复用、资源并行、结果覆盖、阻塞及语义风险。它是决策材料，不是正式 Target、frontier、Run 或 receipt。先形成可启动的局部工作，再随已接纳结果补充，不要求一次列尽。

RG 冻结 Target 的 ExperimentKey、目标、输入、cells、初始 implementation_revision_ref、承诺和依赖。实际 Target 工作按所用方法、实现版本和输入如实交接；初始方法建议不锁死跨 Baseline 归属。仅真正消费新产物或以其自适应选择后续路线时建立依赖；共享已接纳材料可以并行。

每个新候选明确 `risk_class=normal | high`。已有授权内的本地实施、调试、研究、评估与保存为 normal；GPU、长耗时、已授权健康数据和大产物本身不要求再次授权。尚未批准的破坏性修改、受限数据外传、访问权限变更、外部费用或超出资源／用途范围为 high，说明具体动作和最小缺少权限。边界不明先核对已接纳输入及授权事实；已授权的独立部分继续。

已接纳 spec 与风险记录保持不可变。技术重试、页面刷新、Attempt／Fence 轮换和恢复同一执行不扩大授权范围；明确拒绝、撤销或范围改变由 Owner 处理。运行／完成事实与 admission 投影冲突时请求 Owner 对账，继续其他可行工作，否则等待具体冲突解决，不生成重复 Target 或虚假人类请求。

## 方法、实施与评价

领域身份按真实方法与因果语义划分，不按文件数、进程数、工具次数或并发划分：

| 对象 | 真实含义 |
| --- | --- |
| Baseline | 可复用方法、对象、输入输出关系及适用边界，可含材料来源与获取方法。 |
| Variant | 具体配方、干预、材料处理或状态形成方式。 |
| VariantRun | 精确输入下的实际实施、取得材料与状态。 |
| ProtocolVersion | 证据、评价／论证程序、指标、停止与聚合语义。 |
| Evaluation | 精确 Variant × ProtocolVersion 配对。 |
| EvaluationAttempt | 对指定 Run／状态的一次实际评价及报告。 |
| MetricResult | 已接纳成功评价的声明指标与结果文档，可为空指标报告。 |

一个 Target 可以有多个 Run、跨 Baseline 方法、交替评价或无评价工作。实际实施可先交接为待评价事实，只有真实评价才生成 EvaluationAttempt；失败评价不补造 MetricResult。定量、定性、理论或报告式评价按具体研究标准表达。

沿用 `baseline_forward_contract`、`variant_recipe`、`evaluation_data`、`split`、`preprocessing` 等字段，不意味着必须计算建模；不适用项用有理由的非空对象表达。required Metric 可为数量、类别、布尔或允许的缺测，纯报告 Protocol 可无指标，结果和分析保存论证与局限。`result_schema` 初始实例形状是指导，Protocol 指标集合、合法 JSON、数值安全、身份及来源仍须有效。

先用 `research_graph.baselines.page`／`read` 发现并读完整候选方法。复用声明 `{"baseline_ref":"实际读取的引用"}`；新方法可声明 `{"method_key":"稳定名称","method_version":"1","method_contract":{"meaning":"方法含义","inputs":{},"outputs":{}}}`，输入输出按领域组织。方法实质改变才形成新版本或方法；换数据、上游 Commit 或备注不改变方法身份。相似候选只帮助发现，不自动合并。

Variant 保存具体配方；Protocol 保存评价规则和稳定划分／预处理语义。本轮精确数据、实现、输入和产物由运行绑定携带，必要的附带 `baseline_forward_contract.run_bindings` 不进入方法身份也不替代正式输入。`code_changed` 表示实际 implementation 内容变化，包含非代码方法材料。

`implementation/` 可以是规程、方法说明、推导或代码；checkpoint 是可复用研究状态，不限于模型权重。按价值、复核、复用与成本保存多个、部分或不保存；`checkpoint_policy` 的 required／forbidden 是建议，不作产物有无门槛。正式 Run、生产者归属、局部输入和具体交接格式由 Target 的正式工作交接 reference 说明，Target 按其运行时提供的入口读取实际 schema。

技术重试或调试不自动产生新 Run／评价；独立实施或独立评价才按真实身份登记。seed、replicate、k-fold 等若由冻结 Protocol 定义完整 part 集及聚合且不相互自适应影响，可作为同一评价；被独立消费或用于改变后续状态时按实际因果关系区分。准备检查只证明自身事实，不能覆盖要求实质研究的 cell。

## 历史输入与精确复用

当前 FormalPlan 的已选证据给出来源 TargetCommit 及资产索引。按新 Target 实际用途读取数据、划分、预测、实现和协议；结果或 notes 的阅读不等于输入绑定。将所需 Evidence、精确 AssetVersion 或由该 Commit 冻结版本的 Asset 放入 `direct_accepted_input_asset_refs`，由系统验证来源及解析配套内容。

资产索引不是整包照搬要求，Baseline／实现验证也不能证明历史数据已准入。缺必需输入时指出具体来源和缺口。图内依赖只指本图 Target，历史输入沿正式资产路径继承；新图 cells 只表本轮承诺，不说明全库是否有旧结果。

实现不是计划变量时，比较绑定同一 held-fixed 精确内容。保留实际生成各 Run 结果的实现；若必要修复改变 held-fixed 承诺，升级语义修订而非静默替换。

## Target 执行、恢复与交接

Target 是 RG 工作身份，TargetRun 是 AR 可恢复执行，根 Session 是 Harness 当前唯一实施会话。根负责方法、实施、观察、局部修订、保留策略及最终交接；可给原生子智能体明确范围和授权，按文件或对象划分写入，仍由根作整体判断。长期观察任务可聚焦日志，不新增 daemon 策略控制层。

daemon 仅 claim、wake、single-flight、cancel、reconcile 和事件转发。claim 只授予当前执行资格，不批准结果；wake 只提示重新读取权威事实。stdout、进度和工具事件供观察，不形成 Metric 或 receipt。

恢复先对账在途命令、未知效果和 durable checkpoint。优先继续同一 claim／Session／workspace；无法恢复时由 AR 退役旧身份并建立明确后继，保持单一 current 根。取消由授权请求触发，待真实 Harness 确认和清理；取消前已完整冻结的交接按其精确身份核验，迟到输出和活动路径不越过边界。

冻结前满足：候选对应实际实现，命令与写入已对账，合同／输入／spec 仍可验证，独立审阅和根自查已完成，所选路径稳定。中间产物留可恢复工作区，正式发布发生在 completion handoff 之后。root 不生成 Owner 身份或 receipt。

AR 核实 current 执行与终态交接；RM 接纳所选真实内容；RG 据冻结合同和 `formal_runs` 登记真实方法、Run、评价／待评价事实及产物角色，再接纳 TargetCommit。可有多个实际测量，也可没有评价；不要求每个 Target 都产生唯一 MetricResult。Bundle 重读接纳事实后更新 coverage。结果已接纳但 Dataset／Environment 尚未登记时，按主 Skill 的整理步骤处理并读回，不重复实施。

## Bundle 收口

| disposition | 依据 |
| --- | --- |
| `realized` | 所需 cells 有 current accepted TargetCommit，结果方向不限。 |
| `blocked` | 技术、权限、资源、currentness 或未知结果阻止可信继续。 |
| `replan_required` | 剩余有效路线必须改变冻结科学承诺。 |
| `ExhaustionProposal` | 冻结范围内已无实质不同路线，全部执行、交接、取消、人类待办和未知效果已对账。 |

负、零、不显著或不确定结果可以实现承诺；真实未完成工作不能靠耗尽或修订标签隐藏。BundleReport 候选包含当前 request／Plan、逐 Brief 覆盖、已接纳 Commit 与交接来源、剩余工作及真实阻塞。它不把 live workspace、未冻结指标或子会话全文作为正式结果。AE 在当前范围与接纳链验证后形成 StageCommit。
