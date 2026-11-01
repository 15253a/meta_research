# Bundle 契约

## 调用闭包与阶段范围

`BundleStageRunRequest` 冻结 stage request、Cycle、epoch、Bundle Run、根 Fence、ContextPack ref／hash、已接纳 FormalPlan ref／内容 hash／receipt、EvidenceReuseSet、GapSet、完整 Brief 列表，以及 Quest 授权、Harness 和资源边界。消费 current FormalPlan 的精确内容，稳定 ref 本身不替代绑定；草稿或漂移的 latest 不能启用 Target。

每次业务 user 开头按[本次阶段定位](../../references/cycle-work.md#本次阶段定位)直接说明当前请求绑定的 Cycle 身份／轮次、Bundle 阶段与 `cycle_focus` 原文，并保留实际来源；dispatch、target-batch 与恢复仍属于该 Bundle 阶段。本轮同一重心随任务说明/notes 交给各 Target，帮助理解所承担工作的研究用意；不新增 Target 冻结科学合同字段，也不替代 Plan、Target 的实际责任。

正式 Plan 已完成规划判断且 Brief 列表为空时，由 AE 验证精确正式 Plan 的 `no_new_experiment_required` disposition 及既有正式接纳事实，形成 `StageCommit(Skipped)`，不建立 Bundle Run 或 Target。该 Plan 可以全部 covered，也可以保留有依据、本轮未安排的 gap；跳过只表示本轮没有 Bundle 工作，完整责任、支持、缺口和不安排理由仍交 Reasoning。Brief 非空则进入 Bundle，即使另有未安排 gap。覆盖与安排的判断依据见[Plan 合同](../../plan_stage/references/contract.md#brief后继和最终交接)。冻结核心承诺的实质改变通过 `replan_required` 交给 Reasoning 与后继 Cycle。

## 从 Plan 到正式工作

首轮完整阅读 FormalPlan 的 answer_contract、coverage、experiment_briefs、notes 及 source_bindings.selected_evidence_bindings，并保留其精确身份与来源；这些必要正文由装配层完整交付，不经通用 preview 的深度、列表或字节裁剪。EvidenceReuseSet 只表达 EvidenceUse 的派生结果，不用于重建或裁剪全部采用来源。Question、前驱接续目的、本轮 cycle_focus 及来源、适用指导与授权说明当前研究背景；frontier、在途工作和已接纳结果说明当前执行事实，二者分别对待。

原 IdeaSet 在首轮保留准确绑定和读取入口，遇到构想用意、依据或适用边界需要核实时回读原文；首轮不额外展开整份 IdeaSet。完整 FormalPlan 的正式科学承诺继续作为安排工作的依据，按需读取不省略必要核对。

Plan 提供每份 Brief 的 goal、characteristics、boundary_constraints、semantic_delta；Bundle 原样承接这些语义，并在 normalization_inputs 为全部 Brief 推导 required_measurement_unit_keys 与 held_fixed_slots。cell 描述所列 Brief 中应完成和报告的工作责任，不预设正向结果或达标；Brief 可以只推进关联 gap 的一部分。未安排 gap 不生成虚假 Brief、cell 或 Target；它们保留在完整 Plan 中，其真实不足和不安排理由不因 Bundle 工作而丢失。准备工作可有自己的责任，但它只覆盖准备事实；实质研究义务仍须由对应研究结果支撑。Protocol 把实际评价、缺测、停止和汇总规则说清，保持 Plan 已明确的比较条件；未被固定的实施细节按研究需要决定。

cells 的切分在首次规范化接纳前由 Bundle 确定；现行滚动合同接纳后保留同一 completion_contract_hash，后续批次追加候选，不重写已冻结的 cell 身份或已接纳 Target spec。尚未提交的候选、实施选择与顺序可据结果调整。一 Target 恰一个 cell 是当前接口的粒度，不是对研究工作本质或 Brief 数量的规定；本设计沿用该接口，不新增修改已接纳规范化合同的路径。

## 策略、粒度与风险

Bundle 在 Session 内维护可修改的滚动策略：未解 Brief／cells、已接纳上游闭包、候选和真实依赖、方法复用、资源并行、结果覆盖、阻塞及语义风险。它是决策材料，不是正式 Target、frontier、Run 或 receipt。可先形成并开展一批 Target；该批工作结束、结果完成必要接纳与读取后，再根据实际所得和剩余责任决定下一批，或判断是否收口。批内独立工作可并行，后续工作沿真实依赖安排；起步不要求列尽未来所有 Target。

RG 冻结 Target 的 `experiment_keys`、目标、输入、cells、初始 implementation_revision_ref、承诺和依赖。Plan 可以有多份 Brief；一份 Brief 可由多个 Target 实现，一个 Target 可履行多份 Brief 共有的责任。现有候选允许多个 experiment_keys，但每 Target 的 measurement_unit_keys 恰含一个 cell，且该 cell 必须被每份所列 Brief 要求；其内部可以有多个实际 Run。映射按实际责任成立，不为凑固定数量拆分或合并。

保持 Plan 的科学目的、支持要求、增量与明确固定边界；已安排工作的真实前置条件不能因其被称为未安排 gap 而省略，后续发现影响冻结承诺或依赖的变化按既有调整／replan 职责处理；Characteristics 中的普通实现建议可据真实情况调整，理由写入既有策略 notes。实际 Target 工作按所用方法、实现版本和输入如实交接；初始方法建议不锁死跨 Baseline 归属。调整不改写已固定的指标、比较条件或 held-fixed 内容。仅真正消费新产物或以其自适应选择后续路线时建立依赖；共享已接纳材料可以并行。

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

按[研究导航](../../references/research-navigation.md#baseline复用方法实施与评价)从相关命中或准确引用展开，复用前读完整相关方法。复用声明 `{"baseline_ref":"实际读取的引用"}`；新方法可声明 `{"method_key":"稳定名称","method_version":"1","method_contract":{"meaning":"方法含义","inputs":{},"outputs":{}}}`，输入输出按领域组织。方法实质改变才形成新版本或方法；换数据、上游 Commit 或备注不改变方法身份。相似候选只帮助发现，不自动合并。

Variant 保存具体配方；Protocol 保存评价规则和稳定划分／预处理语义。本轮精确数据、实现、输入和产物由运行绑定携带，必要的附带 `baseline_forward_contract.run_bindings` 不进入方法身份也不替代正式输入。`code_changed` 表示实际 implementation 内容变化，包含非代码方法材料。

`implementation/` 可以是规程、方法说明、推导或代码；checkpoint 是可复用研究状态，不限于模型权重。按价值、复核、复用与成本保存多个、部分或不保存；`checkpoint_policy` 的 required／forbidden 是建议，不作产物有无门槛。正式 Run、生产者归属、局部输入和具体交接格式由 Target 的正式工作交接 reference 说明，Target 按其运行时提供的入口读取实际 schema。

技术重试或调试不自动产生新 Run／评价；独立实施或独立评价才按真实身份登记。seed、replicate、k-fold 等若由冻结 Protocol 定义完整 part 集及聚合且不相互自适应影响，可作为同一评价；被独立消费或用于改变后续状态时按实际因果关系区分。准备检查只证明自身事实，不能覆盖要求实质研究的 cell。

## 历史输入与精确复用

按 Plan 修订合同，当前 FormalPlan 的 `source_bindings.selected_evidence_bindings` 保留完整采用来源；Bundle 按各 Brief 可选的 `reuse_material_refs` 以 evidence_ref 定位准确 reader，再按新 Target 实际用途选择所需材料。完整采用来源沿正式 Plan 绑定消费；引用目录不是必须全量采用的执行输入，也不以 EvidenceReuseSet 代替复用材料列表。

按实际用途读取数据、划分、预测、实现和协议；结果或 notes 的阅读不等于输入绑定。将所需 Evidence、精确 AssetVersion 或由来源 Commit 冻结版本的 Asset 放入 `direct_accepted_input_asset_refs`，由系统验证来源及解析配套内容。未采用的参考材料可保留其研究背景作用，不伪称实际使用。

资产索引不是整包照搬要求，Baseline／实现验证也不能证明历史数据已准入。缺必需输入时指出具体来源和缺口。图内依赖只指本图 Target，历史输入沿正式资产路径继承；新图 cells 只表本轮承诺，不说明全库是否有旧结果。

实现不是计划变量时，比较绑定同一 held-fixed 精确内容。保留实际生成各 Run 结果的实现；若必要修复改变 held-fixed 承诺，升级语义修订而非静默替换。

## 准确材料交接

Target 的任务说明、正式合同与正式输入共同承接实际工作。必要的短规程、固定比较条件和研究用意可写入本 Target 准确的正式任务／measurement_contract 内容；长规程、数据或实现按其真实状态交付：已有接纳材料沿精确 AssetVersion／来源 Commit 绑定；同轮上游新产物先完成其真实接纳，再作为实际依赖提供给消费它的 Target。普通参考材料沿获授 reader 阅读，不因阅读而声称已作为执行输入使用。

派发 Target 时，将已知且会影响本项实施、输入核对、评价或结果解释的关键事实、适用条件与限制，连同准确来源写入既有任务说明／notes；关联 Brief 的开放选择已由本项合同落实时，交代已定条件。按本项责任选择这些内容，完整上游背景与长材料保留准确读取入口。Target 接收时能直接理解关键情况；需要具体原件的工作仍按正式输入与获授 reader 核对。

Bundle 工作台中的临时文件是草稿或参考来源。仅给出该路径不能证明 Target 可以读取，也不能把它伪装成已接纳资产。若正式工作必需该内容，先把必要语义落实到 Target 的正式任务／合同，或沿现有材料接纳与授权读取路径完成准确交接；需要实际产生、验证或整理新材料的工作可由具有明确责任的 Target 承担。必需原件或权限仍缺时说明具体缺口，受影响工作等待，独立 ready 工作继续。

完整来源消费已沿 Plan／Target 交接提供；工作区读取只使用当前实际获授的入口，跨 Cycle 三轮原生目录窗口尚待目录能力接通。工作台可见性本身不替代正式输入绑定。Target 不经全盘扫描、私有数据库或放开 Bundle 目录权限补齐缺材。

## Target 执行、恢复与交接

Target 是 RG 工作身份，TargetRun 是 AR 可恢复执行，根 Session 是 Harness 当前唯一实施会话。根负责方法、实施、观察、局部修订、保留策略及最终交接；可给原生子智能体明确范围和授权，按文件或对象划分写入，仍由根作整体判断。长期观察任务可聚焦日志，不新增 daemon 策略控制层。

daemon 仅 claim、wake、single-flight、cancel、reconcile 和事件转发。claim 只授予当前执行资格，不批准结果；wake 只提示重新读取权威事实。stdout、进度和工具事件供观察，不形成 Metric 或 receipt。

恢复先对账在途命令、未知效果和 durable checkpoint。优先继续同一 claim／Session／workspace；无法恢复时由 AR 退役旧身份并建立明确后继，保持单一 current 根。取消由授权请求触发，待真实 Harness 确认和清理；取消前已完整冻结的交接按其精确身份核验，迟到输出和活动路径不越过边界。

冻结前满足：候选对应实际实现，命令与写入已对账，合同／输入／spec 仍可验证，独立审阅和根自查已完成，所选路径稳定。中间产物留可恢复工作区，正式发布发生在 completion handoff 之后。root 不生成 Owner 身份或 receipt。

AR 核实 current 执行与终态交接；RM 接纳所选真实内容；RG 据冻结合同和 `formal_runs` 登记真实方法、Run、评价／待评价事实及产物角色，再接纳 TargetCommit。可有多个实际测量，也可没有评价；不要求每个 Target 都产生唯一 MetricResult。Bundle 重读接纳事实后更新 coverage。结果已接纳但 Dataset／Environment／Literature 的必要登记或关联尚未完成时，按主 Skill 的整理步骤处理并读回，不重复实施。

## Bundle 收口

普通 Literature 登记、用途关联及 Target 阅读判断索引尚待 #270 接通。接口缺失不是无候选：BundleReport 如实保留未完成的整理步骤、已有文献来源与阅读判断及具体阻塞，不能以空候选或阶段交接冒充文献整理完成。

| disposition | 依据 |
| --- | --- |
| `realized` | 已安排 Brief 的所需 cells 有 current accepted TargetCommit，结果方向不限；不据此宣布 Plan 中未安排的 gap 已履行。 |
| `blocked` | 技术、权限、资源、currentness 或未知结果阻止可信继续。 |
| `replan_required` | 剩余有效路线必须改变冻结科学承诺。 |
| `ExhaustionProposal` | 冻结范围内已无实质不同路线，全部执行、交接、取消、人类待办和未知效果已对账。 |

负、零、不显著或不确定结果可以实现承诺；真实未完成工作不能靠耗尽或修订标签隐藏。BundleReport 候选包含当前 request／Plan、逐 Brief 覆盖、已接纳 Commit 与交接来源、剩余工作及真实阻塞。它不把 live workspace、未冻结指标或子会话全文作为正式结果。AE 在当前范围与接纳链验证后形成 StageCommit。完整 Plan 中未安排的责任与理由继续供 Reasoning 综合，不另建覆盖台账；新完成的文稿或其他产物据实际 TargetCommit 报告，不回写旧 Plan 的覆盖和来源快照。

## 正式输出

首次候选只返回 `{"target_plan": ...}` 或 `{"exhaustion_assessment": ...}` 中一个闭合分支，字段按本次 schema。TargetPlan 使用 formal v3：完整 NormalizedCompletionContract 与初始 StrategyUpdate；先由全部 Brief 推导 `normalization_inputs`、required cells 和 held-fixed slots，候选列表不反向缩小合同。首次 `revision=1`、`requires_accepted_labels=[]`、candidates 非空、`strategy_complete=false`。后续 FormalStrategyUpdate 的 revision 为当前 revision 加一；`requires_accepted_labels` 只引用真实接纳的 label，并同时进入实际自适应候选的 `depends_on_labels`。追加候选保持未封口，最终汇总使用空 candidates 和 true；现有 Target／Commit 保持不变。

每个 formal wrapper 携带明确 `risk_class=normal|high` 与完整领域 `measurement_contract`；canonical candidate 本体不扩展。ExperimentKeys 与唯一 measurement cell 精确一致，implementation revision、held-fixed slot revision、保语义 route、真实 dependency／direct accepted asset refs 准确绑定。risk_class 缺失或未知不能默认为 normal。材料来源、许可和使用限制沿 implementation_content receipt 传递；实际采用和修改如实声明。

measurement_contract 完整表达 Baseline 方法、Variant 配方、Evaluation／ProtocolVersion lineage，以及 evaluation data、split、preprocessing。required／optional Metric definitions 保持顺序，逐项携带 MetricKey 与非空 definition；internal parts 按声明顺序，有 parts 时同时给出 aggregation rule ref 和完整正文，无 parts 则不添加 aggregation。每条 preregistered stop rule 给出 ref 与正文；checkpoint policy、result schema ref 与内容保持完整。这些领域规则不携带 provider、adapter、image、command 或 execution 路由；Target 根据冻结合同自主实施。`result_schema` 为初始表达指导，合法 JSON、Protocol 指标、身份和来源继续核验。

exhaustion_assessment 仅在冻结 FormalPlan 内没有实质不同且可接纳的路线时使用，携带完整 NormalizedCompletionContract；逐 cell 的每条已探索保语义 route 带准确 held-fixed bindings、RouteSpec、完整 RouteDisposition 与核验来源。已拒候选沿本次 predecessor_rejections 及其 hash 完整保留；不存在时为空。可执行 route、semantic barrier、active／blocked／pending／unknown work、HumanRequest、未对账 external operation 或 accepted-unconsumed result 均须先按真实状态处理，不能以失败次数、attempt 上限、耗时、费用或仅一条路线失败证明耗尽。根完成独立审阅并核对后交候选，正式接纳由 Owner 按本次冻结范围与当前事实判断。

每条 route 保留可重算的 frozen fingerprint。未执行、经冻结语义审阅关闭的路线使用 `duplicate_frozen_semantics` 或 `semantically_ineligible`；实际提交且被 Owner 拒绝的路线使用 `attempted_rejected`，其 `evidence_refs` 精确引用本次 issuer-verified predecessor rejection inventory。route 声明的每个 external operation 都需同 subject 的 terminal reconciliation receipt。
