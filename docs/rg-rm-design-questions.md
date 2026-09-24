# RG／RM 资产设计讨论

2026-09-24 补充：本文原有“五入口”设计已由 [ADR 0006](adr/0006-environment-as-reusable-research-index.md) 扩展为含 Environment 的六入口。Question 侧重进展总览，Baseline／Target 和 HumanRequest 侧重过程方法积累，Literature、Dataset、Environment 侧重产物与资源复用。Environment 还包括已有设备、设施及现实物理资源，可按来源直接登记；Target 新产物与 Dataset 同环节整理，其他机器的适配或修复属于新 Target。Baseline 层级及五个 Owner 保持不变，本文历史内容不表示 Environment 已实现。

日期：2026-09-22。状态：Q1–Q4 均已回答；用户要求先看四阶段职责、工具、落盘与交接的具体例子，再逐项评议。当前审阅稿为[四阶段职责、工具、资产与交接](four-stage-asset-workflow.md)，整体设计尚未确认。第一版先完成同一批 Target，过程中提前入库及提前复用优化暂不纳入。下述为设计核对，不代表场景已通过运行验证，也不代表实现或部署完成。

## 已确定的主干

- RG、RM、AE、AR、HC 继续作为深模块；RG 的研究入口为 Question、Baseline、Dataset、Literature、HumanRequest／人类输入。
- Target 实际工作及保留科研产物进入 Baseline 层级；无评价时不制造 EvaluationAttempt 或 MetricResult。Dataset 可为同一产物提供独立数据语义身份。
- RM 共用精确内容读取，RG 组织归属及关系；阶段交接、执行恢复与 Owner 协作留在相应职责内。
- 取消研究材料按类型获得证据资格的要求；保留真实来源、版本、归属与实际评价含义。
- Idea 通过文献与已有研究探索可能的研究方向、假设和方法思路，文献检索、查读和比较可以是主要工作。
- Reasoning 综合发现关联 Question 研究历史；Reasoning 中 DeepFetch 仅用于拟建新问题的路径，返回标准 summary.md 后，由原 Reasoning Session 继续判断新题。
- Reasoning 合理委派阅读、召回与分析；各根 Session 如做 review，必须使用独立子智能体并由主智能体改稿；检索、索引整理、归类或入库工作量较大时，也应合理委派独立子智能体。规则放入 skill，不增加 review 接纳机制。

术语见 [CONTEXT.md](../CONTEXT.md)，设计取舍见 [ADR 0005](adr/0005-research-assets-and-shared-content.md)。

## 当前代码事实

- 一个 Target 已能声明多个 VariantRun；所用 Variant 仍被约束在其执行授权选定的同一 Baseline 下，见 [formal_entities.py](../src/meta_research/formal_entities.py)。不能把“一 Target 一 Run”当作现有模型或用户要求。
- 当前 Bundle 接纳 Target 时，`_insert_target_with_measurement_authority` 就解析或创建并固定主要 Baseline／Variant；Target 完成时，首个实际工作项仍须匹配该 Variant。其他工作项也只能选择已存在且属于同一 Baseline 的 Variant，见 [research_graph.py](../src/meta_research/owners/research_graph.py) 和 [formal_entities.py](../src/meta_research/formal_entities.py)。因此只取消同 Baseline 检查还不够，实际归类与新建能力也必须交给 Target Agent；Bundle 可以提供初始方案，不锁定其全部最终归类。
- 现行完成流程接受无评价 Run，也可记录失败的实际实施；不能根据遗留文案推断无评价工作无法完成。
- 未分配的产物可以只留在 completion manifest，见 [formal_entities.py](../src/meta_research/formal_entities.py) 的 `_register_subject_artifacts`。这与本轮要求的科研产物归类不完整相对应。
- Plan 的证据目录排除没有 accepted assessment 的 TargetCommit，见 [target_commit_evidence.py](../src/meta_research/target_commit_evidence.py) 的 `_has_measurement` 分支。该资格限制与本轮已定要求冲突，无需再决定是否保留。
- RM 已有以精确 version_ref 定位、读取和导出内容的能力；统一 Agent 读取方式应基于该能力收拢现有路径。
- 长 Target 的现行根 Session 主路径在结束交接后才保存 completion 内容，并在 TargetCommit 接纳事务内登记正式 Run 及产物归属，见 [target_run_finalizer.py](../src/meta_research/target_run_finalizer.py) 和 [research_graph.py](../src/meta_research/owners/research_graph.py)。`target_run.progress` 的中间日志读取不等于科研资产已进入 Baseline 入口；这是第二轮 Q4 所涉及的现行限制。
- 技术性入库或接纳失败已有复用 completion／RM manifest 并继续接纳的恢复基础；内容被拒绝可以回到原 Session 修正，不强制重跑科研。该恢复能力不是待新增的科研资产层级。

上述为 canonical 本地源码的只读核查结果，不是对线上研究状态的验证。

## 第一轮决定及其分支

### Q1：Target 在 Baseline 层级中自主检索、复用和积累

已定：允许一个 Target 跨多个 Baseline，Target Agent 根据实际研究含义决定复用或新建 Baseline、Variant、VariantRun 以及实际需要的评价对象。Baseline 层级用于组织和查找科研积累，不预先限定 Target 只能在某个 Baseline 内活动。

- 复用 Baseline A 并采用新配置，可以在 A 下建立新 Variant，实际实施归到相应 Run。
- 深入比较 A、B，若形成独立比较方法，可以建立 Baseline C；不把“比较两个方法”写成必建 C 的硬规则。
- 跨方法报告归到实际产生它的研究工作，并关联使用或讨论的既有材料，具体层级由 Agent 决定，不另建报告分类层。
- 单纯总结、汇总和综合推理可以由 Reasoning 完成，不必制造 Target。
- TargetRun 表达研究活动及恢复、归类入库过程；TargetCommit 表达本轮本阶段交接，科研积累仍从 Baseline 层级读取。
- 具体入库和阶段交接可由 Target 或 Bundle 承担；复杂、耗时的 Target 保留可单独管理的 Session。这不增加新的科研资产层级或交接流程。

原先拟追问的多对象报告归属，已由上述 Agent 决策职责覆盖，不再要求用户逐类制定分类规则。

### Q2：直接纠正当前归属，保留简短理由

已定：允许后续 Agent 调整或移动错误归属，留下理由即可。不维护一套旧分类副本，不复制内容，也不因修正归属新造 Run 或评价。同一精确内容引用保持可用。

这取代第一轮推荐中的“保留原归类”要求；不据此扩张成分类版本管理、审计或审批流程。实际内容发生改变时仍沿 RM 的内容版本能力处理，归类调整本身无需复制正文。

### Q3：保留有意义的阅读差异，保持轻量

已定：同一文献可保留不同研究背景下有意义的阅读判断，也可有综合摘要。各次判断共用所读原文的精确版本，不要求每次阅读都新增完整记录，不复制整套文献或阅读材料。

已有判断可以复用；补充或修正只保留有价值的差异、背景与出处。关联 Question 和原文的具体读取方式属于现有对象及共用内容读取的设计，不再增加独立的阅读资产入口。

## 第二轮决定：第一版先完成同一批 Target

### Q4：过程中提前入库与可见性优化暂缓

已定：用户要求第一版先把同一批 Target 做完，不实施长 Target 中途把部分 Run／产物提前归类入库、供其他 Agent 查找和阅读的优化。继续围绕完成后的资产归类与交接打通正常研究路径。

第一版不设计未完成 Target 的中间产物发布、提前消费以及相应执行依赖调度；原先“允许过程中提前入库”的推荐未被采纳为第一版要求。需要时另行讨论，不作为当前改造的隐含后续工作。

此范围选择不取消正常执行中的文件保存、日志观察或已有恢复能力，也不要求新增整批资产一次原子提交的机制。完成后的真实科研产物仍须正确归到 Baseline 层级，后续可通过统一内容引用读取。

### 不再作为未决问题的细节

- Target 内的具体研究归类、是否形成新比较方法、什么总结适合 Reasoning，已经交给 Agent 判断；不继续逐例制定分类硬规则。
- Target 或 Bundle 承担具体登记和交接均可，不再要求用户为每一步指定执行者。
- 归类调整和多处引用不复制原文；实际不同且已被采用的内容版本，与重复的物理副本有区别。既有保留指引保护唯一原始来源及仍被引用的精确版本，见 [artifact-data-granularity.md](artifact-data-granularity.md)，无须因本轮“少副本”要求重复设计另一套清理机制。

## 完整使用场景核对

逐项核对既有目标与本轮决定后，以下场景均已有领域答案。现有代码尚有差距，不把待实施的功能重新变成用户选择题。

| 场景 | 已定行为 |
| --- | --- |
| Plan 在已有积累中选方法及材料 | 从相应研究入口检索、展开关系并按精确内容引用读原文；阅读不受本轮冻结输入一概限制，实际采用与执行输入按各自职责明确。 |
| Target 沿用 A 方法但改变配置，或深入比较 A、B | Agent 决定复用或建立对应 Baseline、Variant、Run 与实际评价对象；比较不自动要求新建 C，也不强制拆 Target。 |
| 同批 Target 完成并交接 | 保留的科研产物进入对应层级，内容进入 RM；无评价不造结果，TargetCommit 不替代科研归属，第一版不提前发布未完成 Target 的中间产物。 |
| 产物同时值得作为 Dataset 使用 | 建立独立的数据语义身份及适当版本、使用和派生关系，关联实际研究工作并共用原件。 |
| Reasoning 综合既有材料，没有新 Target | 可以结合实际结果、理论分析、文献与人类专业意见形成认识，说明依据及边界，关联 Question 历史；不以材料类型筛除依据。 |
| 后续 Agent 发现归类错误 | 直接调整关系并留简短理由，不复制正文；同一精确内容引用继续可读。 |
| 不同问题对同篇文献形成不同理解，Writing 后来采用 | 轻量保留有价值的判断及背景，共用精确原文；Writing 指向实际使用版本和引用位置，历史来源不被摘要更新静默替换。 |
| 人类未收到请求便主动提供专业意见或材料 | 从人类输入入口可找到与研究相关的输入，意见、事实、授权、材料保持实际含义；一般聊天不自动进入研究资产。 |
| 同一研究目录中的其他 Quest 已有相关材料 | 五入口在同一研究积累范围内提供获授权的发现与读取，保留 Quest 来源；不跨到未登记的邻接目录或旧研究。 |
| Reasoning 拟创建新问题并需要检索支持 | 仅在拟建新题路径按需调用 DeepFetch，返回稳定的 summary.md，由原 Reasoning Session 阅读、调整并决定新题；普通综合和补充阅读不走该路径。 |
| Reasoning 或其他根 Session 的阅读、分析、索引整理及入库量较大 | 合理委派独立子智能体分担，根 Agent 组织和综合，不先把整个资产池读完再委派；不增加新的科研层级。 |
| 根 Session 进行 review | 必须由独立子智能体指出问题，再由主智能体改稿；不新增审核记录或接纳流程。 |

跨阶段阅读、写作来源和研究目录范围的既定依据见 [后端目标形态与改造边界](../../next-step-plan-20260921/8768后端目标形态与改造边界.md) 的 2.4、3、5、6 节。这些约束不是本次核对新提出的机制。

### Q5：整体使用方式核对

待用户核对：第一版是否以“找到已有方法和原文 → Target 自主复用或建立适当层级对象 → 同批工作完成后完成科研资产归类及交接 → Reasoning 综合并决定后继 → 后续 Agent 从研究入口找到并继续使用这些积累”为完整主线？

推荐：用这条主线及上表作为第一版行为与验收场景，不额外扩展中途发布等已延期能力。此问题是整体理解确认；如果用户指出遗漏或误解，再据此展开相应设计分支，不预设更多问题。
