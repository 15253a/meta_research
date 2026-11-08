# 研究导航：六个研究视角

查找已有工作、追溯依据或选择可复用积累时使用本文。研究积累同时具有过程、内容、用途和来源；导航使你从当前需要的角度进入，再沿关系理解整体。已有准确正文、路径或读取入口时，可以直接使用。

## 六个并列入口

RG 通过 Question、Baseline、Literature、Environment、Dataset、Human Interact 六个并列入口组织历史研究发现。每个入口回答不同的研究疑问，同一内容可以在多个视角中获得不同含义。按当前需要选择相关入口，无须每轮遍历，也无须把每份材料关联全部六类。

当前协作和交接沿 HC、WF 接续。近期工作通过宿主提供的当前与前两轮获授目录，用原生文件工具查找；需要查找研究积累、理解关系与来路时使用 RG。入口由眼前的问题与已知线索决定，不按材料新旧强制分流，也没有固定的 Workspace、RG 搜索顺序。

## Question：理解问题与研究历程

需要理解工作为何开展、已有何种认识或还存在哪些未知时，从 Question 进入。沿问题与 Cycle 的关系读取实际工作、结果、失败、负结果和未决事项，理解当时的研究目标、采取的路线和继续或转向的依据。

当前 Quest 内其他问题、暂放问题及过往 Cycle 的积累，都可按相关性复用。需要发现历史 IdeaSet 时，从其 Question 与 Cycle 关系取得准确入口；已有正式绑定或准确引用时直接读取。问题的状态与摘要帮助定位，重要判断仍以所需正文和实际工作为依据。新未知的组织与正式建题见 [新问题与后继 Cycle](cycle-work.md#新问题与后继-cycle)。

## Baseline：复用方法、实施与评价

需要理解一项工作怎样开展、为什么这样做、实际得到什么反馈时，从 Baseline 视角进入。Target 承担有目的、有实际执行和反馈的工作，其完整执行脉络通常沿这个通用方法层级登记。下载资料、分析、数据清洗与预处理、汇报材料准备、算法运行、环境搭建、系统模块实现等，都可以由此组织。

| 对象 | 研究含义 |
| --- | --- |
| Baseline | 方法的目的、输入输出关系与适用边界。 |
| Variant | 采用的具体方案、配方或做法。 |
| VariantRun | 一次真实执行，以及实际输入、实现与处理过程、输出产物、结果和反馈。 |
| Evaluation | 对方案及精确 ProtocolVersion 所定义的评价。 |
| EvaluationAttempt | 针对指定执行或产物开展的实际评价。 |
| MetricResult | 评价实际得到的数值指标，适用时记录。 |

`Baseline → Variant → VariantRun` 使方法、方案与实践相互关联；评价进一步说明所得结果在什么条件下有什么意义。评价可以是定性的，也可以包含数值指标。保留未评价的事实，区分评价定义、实际评价与所得结论。

**目的导向、经验参照、反思修正。** 既有层级承载长期经验，为研究提供稳定参照。Target 可以采用多种方法、进行多次尝试；工作与登记粒度由研究含义决定。依据目的、当前理解和证据，自主选择组织方式、保留范围和登记数量；理解深化时有据调整，并保留实际工作及新旧归属的对应关系。需要独立比较、评价或复用的真实工作保持可区分。

先理解问题与实际工作的联系，再在既有对象含义和关系中选择合宜的组织方式，让研究思考能够跨越单一分类展开。

检索按当前已提供的范围发现相关研究描述，再回溯归属或展开关联。Baseline 检索覆盖方法合同、Variant recipe、Evaluation protocol、VariantRun／EvaluationAttempt 的正式输入描述与结果 manifest，以及 Artifact 的角色和 RM 描述。命中只提供候选；使用 baselines.read 的 relation 分别展开 variants、runs、evaluations、attempts、metrics、artifacts，并用返回 reader 读取准确记录。各关系独立续页，真实 Run 可以没有评价。已有准确引用时直接读取。复用前理解方法的目的与条件，沿实际输入、处理过程、产物和评价判断适用性；相近的方法或相同的内容不能代替执行依据。Target 的声明、修订与正式接纳见 [正式工作交接](../target-execution/references/formal-work.md)。

## Literature：理解外部研究及已有阅读

需要理解研究现状、查证论述、形成构想或接续阅读时，发现文献、准确原文及已有阅读判断。沿同一文献在不同问题中的采用背景，理解它支持什么主张、存在什么限制、曾引发什么分歧。

Target 执行中也可能发现、取得并阅读文献；值得保留的文献身份、原文入口、本次判断及用途按[资源整理与正式登记](materials-and-custody.md#资源整理与正式登记)交接，使后续阶段能够继续阅读和复用。

原文、先前解释与当前判断各有来源。已有摘要帮助选择阅读方向，当前关键主张则按需要回到原文验证；阅读的深度随研究问题和反馈逐步展开。

## Environment：理解可用研究资源

需要计算、交互、仿真或现实资源时，发现环境、设备设施、场地、持久目录、安装与服务。沿资源身份、来源、能力、描述快照和适配来路，理解已有使用知识及研究用途。

登记保存已知认识，实际使用仍需核实本次可用性、权限与条件。没有数字文件的资源也有研究含义；资源说明及其 hash 只标识说明，实体当前状态由实际观察确认。资源保管见 [资源整理与正式登记](materials-and-custody.md#资源整理与正式登记)。

## Dataset：理解数据身份、版本与派生

需要选择或复用数据时，理解其语义身份、精确版本、来源，以及清洗、标注、切分、合并等实际派生关系。结合各问题中的用途、处理条件与局限，判断哪些内容仍可支持当前研究。

数据身份、实际处理和研究用途相互关联。使用时取得所需精确版本与划分，保留多来源来路，使后继能够复用具体内容并理解它如何形成。

## Human Interact：理解人类协作及其研究影响

需要发现人类协作历史、追溯要求来路或回顾协作经验时，从 Human Interact 视角理解请求、回复、已确认指导、主动材料及其处理与研究影响。沿本次获授的 `human_request.read` 查请求、回复、主动提交材料；entry_kind=guidance 查正式提交的指导，再用目录返回的 constraint_ref／version_ref 纯读其正文、约束力度与作用范围。历史读取不会领取指导或标为已读；当前指导的交付处理仍沿 human_guidance 操作。助手未正式提交的交流不进入这份指导历史。读取人的原文与必要背景，区分意见、授权、适用要求与实际结果，理解采纳理由、有效条件和未决事项。当前求助、适用指导及已送达答复可沿 HC 的准确入口直接接续。

HC 保存协作原记录与流程，RG 组织研究关联。HC、系统工作和 Agent 登记均可提供关系来源，按实际来路保留。当前协作的发起、回应和指导处理见 [人类协作](human-guidance.md)。

## 跨入口关联与准确读取

同一工作可以在多个视角中被理解。例如，清洗数据时，Baseline 层级关联原始输入、处理方案、实际执行与新数据产物，Dataset 同时组织新数据的身份、版本和派生关系。两边共用同一份 RM 精确内容，分别支持理解执行与复用数据。

沿准确引用、版本和读取入口取得当前所需依据，按返回的分页与工具合同继续。关系登记表达内容为何有用，RM 保管内容本身；增加用途无需复制正文，也不另造生产或执行事实。当前 Quest 外的内容沿明确授权和正式引用使用。

目标演化后，回到工作当时的背景，理解什么证据促成了转向、哪些认识在新目标下仍然适用。保留旧目标、历史依据与真实分歧，使新的理解能够解释自己的来路。登记与保管见 [材料与保管](materials-and-custody.md#资源整理与正式登记)。

工具按研究用途提供渐进索引：RG 查研究积累与关系，RM 读准确正文和保管材料，WF 查流程、交接与原操作结果，HC 处理当前人类协作。用途可跨 Owner，同一真实操作可从相关用途到达。工作目录使用宿主给出的路径和原生文件／shell，外部 MCP 使用实际已配置且获授的服务；各能力仍沿原职责和授权。

## 当前获授工具与准确入口

首包给当前根的用途概览和少量常用操作摘要。需要寻找操作时，`tools.catalog.page` 返回同一概览；按返回的 `reader` 展开某个 `purpose`，再按需选择 `topic`。RG 的 Question、Baseline、Literature、Dataset、Environment、Human Interact 连接研究历史与准确 reader；HC 面向当前求助、回复和正式指导。已知线索可直接按 `query` 搜索，或按 `owning_module` 筛选；确需枚举全部获授操作才用 `view="operations"`。操作页沿 `next_offset` 与同一 `catalog_hash` 继续，保留原筛选；`next_offset=null` 表示本次目录已读完。`tool_catalog_changed` 要求从首条重读，目录不冻结后续许可。

需要核对参数、返回或错误时，以准确 `operation_id` 调 `tools.contracts.read`，读取现行 `inputSchema`、`outputSchema`、返回说明及错误合同。执行用 `tools.call(operation_id, arguments)`；reader 的 `operation` 是操作身份，其余原参数放入 `arguments`。已知准确入口可直接执行，无须先发现或重复读取合同；原操作名的协议调用同样有效。模型工具列表只提供这些入口，目录、合同和执行共用原注册、当前授权与 handler。未注册、禁用或未获授按真实原因反馈，不当作合法空结果。参数可修正时沿具体诊断修改自己的提交；效果未知先按准确合同对账原操作。原生文件、shell、子 Agent 沿宿主实际合同使用，不增加 RW 导航层。

外部 MCP 先按已配置服务发现：`external_tools_catalog` 给服务及少量操作摘要，沿返回入口展开；`external_tools_contract` 按准确工具身份读取 schema 与服务说明，`external_tools_call` 调用真实工具。已知身份可直接调用。未配置、未获授和服务故障保持实际状态，外部结果不替代 Owner 接纳；恢复见[外部 MCP 故障协作与恢复](../external-mcp-recovery.md)。

先读当前研究交接。当前 Quest 内可跨 Question、跨 Cycle 检索已接纳材料，包括暂放问题的历史；按实际研究需要选择入口，不要求每轮遍历全部入口。Question 用 research_graph.questions.page 从名称/关键词发现，再用 research_graph.question_history.read 分页看历史 Cycle 与各阶段；沿阶段 reader 读取准确 RM 内容，Bundle 沿 AE 已存 closure／report／notes 读取，保留原同轮交接；Baseline 用 research_graph.baselines.page/read 展开方法、Variant、Run 和评价；Dataset 用 research_graph.datasets.page/read 查含义、版本与派生关系；Environment 用 research_graph.environments.page/read 查环境、设备设施及现实资源的含义、来源、条件和精确内容引用；Literature 用 research_memory.literature.page 查原文和不同 Question 的阅读判断；人类输入用 human_request.read 查看请求、回复与主动提交。目录沿 next_offset 与 query_revision 继续，保持查询、过滤与页大小；请求目录沿 next_cursor 继续；各请求的 responses_reader 用 entry_kind=responses、request_ref、response_offset/query_revision 独立查看全部回复。相关 Owner 记录插入、删除或变化时返回 research_query_changed，从首项重开查询；无关 Quest 的变化不使当前续页失效。result_state=page_pending 表示仍在扫描，exhausted 表示本次续页结束；只有首个完整空查询才可报告 no_match（有检索词）或 no_records（无检索词）。不可见、content_unavailable、content_drifted 分别是授权或材料问题，不等于没有研究积累。reader 含 operation 时调用其指明的工具及参数，如 history_reader／formal_results reader；共用正文 reader 的 source_ref／version_ref 才交给 research_memory.content.read，并沿返回的字节偏移读取精确原件。大文件与 linked_local 直接使用受控原位置，不为参考阅读复制全份材料；发现与读取限于当前 Quest 可见范围，已知 Environment 的显式跨 Quest 引用按下文规则办理，其他资产仍沿原 Owner 权限与接纳边界。结论摘要、结果 JSON 或 hash 不等于可继续实施的完整材料：复用时核对实际需要的数据、划分、预测、方法实现和协议，经现有 Owner 引用与执行输入流程交接精确资产。六入口按查看或复用目的组织：Question 汇总研究进展，Baseline 展开真实方法与实施，人类输入保留协作过程与经验，Literature、Dataset、Environment 便于复用已有资源；入口不是互斥分类，同一原件可关联多个入口。只带入本阶段需要的内容与来源，保留完整历史的按需读取入口。

正文、目录与阶段上下文的分页和错误处理见[准确读取与错误](materials-and-custody.md#准确读取与错误)。正式采用集合与五类来源绑定沿[Plan 的 Owner 操作](../plan_stage/references/owner-operations.md#发现读取和精确引用)处理；可读与正式采用分别判断。

### Dataset 与 Environment 登记

数据是研究的重要基础，优先保护昂贵或不可替代的原始数据，记录来源、处理方法及可重建条件；清理临时文件前确认是否仍是唯一可用副本。通过 research_graph.datasets.page 检索已有语义身份，用 research_graph.datasets.read 按需读取精确版本与来源关系。确需登记时，用 research_graph.datasets.register 记录名称与含义，用 research_graph.datasets.register_version 绑定 RM 已接纳的精确资产版本；使用时用 research_graph.datasets.reference 引用到当前 Question，purpose 和 notes 说明用途与局限。对有必要保留的清洗、标注、切分或合并关系，用 research_graph.datasets.derive 关联精确来源与派生版本并说明处理方法，多来源分别关联；由 Agent 判断登记粒度。每个新 DatasetVersion 完成后，用 datasets.reference 关联当前 Question，purpose说明真实研究用途；结果不明时先 reconcile，接着已完成的步骤继续，不重跑科研或重复造版本。RG 记录身份、用途和来源，RM 管实际内容与资产版本；登记关系不会复制或备份原文件，检索发现仍需经过已有正式证据和执行输入流程才能用于冻结研究。

Environment 保存可复用研究资源的含义、来源与使用知识，可包括交互／仿真环境、运行环境、现有 GPU 或其他设备设施、场地。已有资源用 research_graph.environments.register 按真实来源直接登记；metadata 保留位置、能力、已知条件及使用说明，asset_bindings 引用已接纳数字材料，无数字材料时可为空。现成持久目录、安装或服务无需先跨机器打包或提供完整重建配方，现实资源无需补造构建 Target。索引快照保留原描述；描述或所引用数字内容变化时登记新 environment_ref，以 semantic_key 归组同一资源，适配成果用 source_environment_ref 关联原环境。一次启动或选卡本身无需新建资源快照。

采用资源时用 research_graph.environments.reference 关联当前 question_ref、适用 research_ref 和 purpose，并读回用途。已知另一 Quest 的精确 environment_ref 时，可显式 reference 到当前 Question；Owner 核验来源快照及每个数字 binding 在目标 Quest 的正式接纳，无数字材料的设备按真实来源引用。接纳引用后才从当前 Quest 的 page/read 读取，不据此遍历其他 Quest 或相邻目录。未知效果用同一 effect_id reconcile。登记和用途记录不改变本次运行条件、使用权限或资源实际状态；本轮选卡、预算与文献范围以当前运行条件为准，使用前按任务需要核实资源可用性。说明文件 hash 只标识说明，不证明实体被冻结或已核验。

本 Target 新建或改动的资源先沿真实 Baseline／Variant／VariantRun 保存并正式接纳，再由 Bundle／Reasoning 与 Dataset 在同一整理核验环节登记；接纳前交接 environment_candidates，暂存产物留在工作区。已有资源的直接登记记录其既有来源，不能代替本次成果的正式交接。换机器所需适配或修复作为新 Target 保存并关联原环境，不影响原环境的入库资格；未完成 Target 的中间产物不提前发布或消费。

### 普通 Literature 的登记与阅读用途

整理选中可复用的普通文献时，先沿既有 RM intake／Target 正式交接保管所需原件，再调用 research_memory.literature.register。record_ref 保留准确 DOI 或 URL 身份，title／source 写外部作者、出版及实际获取来源，asset_bindings 只填当前 Quest 已接纳的准确原件；fulltext_status=obtained 需要原件 binding，未取得填 missing／waiting_user，metadata 说明限制与失败。登记不制造 DeepFetch Run，不表示读懂或科学接纳。

用 .reference 将 literature_ref 与 question_ref、purpose、适用 research_ref 关联。reading_summary 记录实际阅读判断、主张、反证与限制；reading_bindings 引用本次分析的已接纳 RM 版本，与外部原文绑定分开。Target 阅读用其真实 target_ref 或 target_commit_ref 及该 Target 已接纳的 research_notes，不能把同 Quest 无关笔记归给它。无需改写原件生产者；普通文献沿 AssetVersion 采用来源进入统一 reader，DeepFetch 文献继续沿 LiteratureSnapshot。

登记／关联用同一 effect_id 的 .reconcile 处理未知效果。用 .read(literature_ref) 查回文献或用途记录，.page 联合发现普通及 DeepFetch 文献；原件在 readers、阅读判断在 judgments，逐个按返回 source_ref／version_ref／entry_path 调 content.read，沿 next_offset 读完。缺原件、失效授权、内容漂移和技术失败按实际错误交接，不解释成无候选。空候选或无保留价值可无记录；已成功步骤读回后继续未完成部分，不重复研究或复制原件。
