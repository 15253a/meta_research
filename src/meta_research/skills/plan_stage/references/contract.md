# Plan 输入、判断与交接合同

配合[主 Skill](../SKILL.md)与[工具和 Owner 操作](owner-operations.md)使用。当前装配已提供准确 Question、完整绑定 IdeaSet、必要前驱及 cycle_focus，完整采用来源和 Brief 派生沿正式合同处理。材料保管与历史发现按[工具合同](tool-contracts.md)使用；工作目录与条件回收沿本文 Workspace 约定，实际访问以宿主提供的路径和授权为准。

## 首轮输入

当前 Plan 会话的首次模型输入按实际入口准确提供当前任务。程序保存或引用权威依据，模型阅读视图直接呈现下表所需正文；身份核验不要求 Agent 手工重建 receipts、hash 或冻结包。

首轮与必要续轮／恢复的业务 user 开头按[本次阶段定位](../../references/cycle-work.md#本次阶段定位)直给“当前你处于第 N 轮 Cycle（请求绑定身份）的 Plan 阶段；当前 cycle focus 是：原文”。序号取绑定 Cycle 在权威历史中的已有顺序，实际没有序号时只显示身份，不从 ref 尾号猜测。此短定位和 focus 来源依同一次请求绑定，不用 latest 重新解释旧 operation；普通工具结果不另造 user 消息。

| 内容 | 直接提供 | 按需读取与边界 |
| --- | --- | --- |
| 当前 Question | 完整已接纳六字段正文：`title`、`unknown_statement`、`answer_shape`、`applicability_scope`（四项核心）与 `background_context`、`requirements_constraints`（两项按需补充，可为空），及准确来源；字段职责见[新问题与后继 Cycle](../../references/cycle-work.md#新问题与后继-cycle)。 | 其他问题、问题关系和历史研究从 RG Question 发现；已知引用可直接读取。Question 保持稳定，本轮投入变化不改写其正文。 |
| 当前 IdeaSet | **一份明确绑定的完整正文，含其中全部 Idea**。正常路径为本 Cycle 前一 Idea 阶段的已接纳产物。 | 前两轮其他集合与草稿按需读取，不自动汇总入首轮；更远构想经 RG Question 查找。 |
| 跳过 Idea 的沿用 | 前驱 Reasoning 明确沿用的准确 IdeaSet、完整正文、原生产来源及沿用依据。 | 来源可早于 Workspace 窗口；本轮正式沿用使它成为本轮输入，既不重写原身份，也不继承旧 Plan 覆盖。 |
| 目标与条件 | 当前适用的完整 Quest 目标、持续条件、研究风格，以及本轮有效的局部条件。 | 目标演化经过、旧目标及关联研究按准确入口读取。 |
| 必要前驱交接 | 实际前驱 Reasoning 针对本轮的接续目的、已有判断及限制、未决问题、选题与入口理由，以及准确来源。首轮新任务没有前驱则明确没有。 | 详细科学报告、历史 notes、执行记录及附属产物按需读取；必要交接不由宿主截取前若干字节替代。经过 Idea 进入 Plan 时同样保留仍适用的交接。 |
| 本轮重心 | 正常后继直接提供上一 Cycle 的 Reasoning 给出的 `cycle_focus` 原文及准确来源；经过或跳过 Idea 都保留。首个 Cycle 的原文与准确起步来源按共享[本轮重心](../../references/cycle-work.md#本轮重心)规则提供。 | 首轮尚未形成与已形成的重心、起步来源及同源传递沿共享规则处理。Idea 与 Plan 继续形成研究切入和具体安排，原 focus 保持原文，取舍理由留既有 notes，不创建 focus 版本。 |
| 有效指导与答复 | 实际适用于本轮的指导原文、范围、力度和有效处理结果；必要的求助答复与接续条件。 | 普通附件沿实际可访问路径用原生能力读取，正式资产沿准确 reader；当前及历史协作沿[人类协作与指导入口](tool-contracts.md#当前工具入口)发现和准确读回。 |
| 材料入口 | 已有准确材料引用、其用途说明与可执行 reader；当前及前两轮 Workspace 的实际可读目录、工作归属与读写范围。 | 脚本、数据、草稿、原件与其他历史正文按研究需要读，不把整个材料目录预先塞入首轮。 |
| 能力入口 | 研究用途概览、工作目录、外部 MCP 和原生能力的可用性及合同取得方式。 | 已知 reader 直接使用；需要发现操作或核对合同才读取 `tools.catalog.page`／`tools.contracts.read`，能力以当前实际授权和配置为准。 |

“全部 Idea”指这一份当前 IdeaSet 内的全部候选，不表示全部历史集合。Question、IdeaSet 与必要交接有明确来源：实际前驱 Reasoning 和 IdeaSet 原生产者是不同关系，不从“最近一份内容”猜测任一关系。

首个 Cycle 的起步来源与同源传递按[本轮重心](../../references/cycle-work.md#本轮重心)处理。正常后继的 `cycle_focus` 若缺失，核对该正式后继的准确输入，不套用首轮起步路径。

首轮不执行 Plan 专用证据预查，也不注入 TargetCommit 首目录。进入 Plan 的后继准备同样不预构造无消费者的 Idea 证据页。AE 的已接纳后继事实读取与各角色输入装配须解耦：沿既有正式事实取得前驱、入口、skip 和沿用绑定，保留目标、指导及材料入口，不新建第二份可独立改写的后继权威。

`PlanStageRunRequest` 仍由系统准确绑定 request、Cycle、epoch、Execution Fence、运行身份、Question 与 IdeaSet 及本次实际适用的输入依据；绑定缺失或接纳事实冲突是技术问题。恢复使用本版运行自己的准确依据，不以执行时的任意最新内容补造原输入，也不为旧任务保留兼容分支。

## Workspace 与正式来源

工作目录按当前 Cycle 和前两轮构成最多三轮窗口；最初不足三轮时保留实际轮数。宿主列出其中本根可达目录、工作归属与读写范围，Agent 按[当前工作目录](../../references/materials-and-custody.md#当前工作目录与回复材料)使用原生工具；每轮可以包含多个目录。窗口外目录清理、`linked_local` 原件及必要父目录的保留与 Session 生命周期见[交接与工作区收尾](../../references/materials-and-custody.md#交接与工作区收尾)。

工作副本供处理，暂存认识供接续思考，正式采用结果有准确内容身份及接纳来源。三者都可能对当前研究有用，但路径可读不等于正式证据。需要保留的研究内容按[材料与保管](../../references/materials-and-custody.md)处理；窗口选择、授权读取和实际回收由系统实现，Plan 不维护遍历或清理证明。独立助手的持久工作台及用户只读原目录按其自身生命周期保留；Writing 沿调用方工作空间的生命周期。

原生文件能力支持近期具体工作，RG 支持研究发现和关系，RM 支持准确正文，HC／WF 支持当前协作与流程接续；它们可以配合使用。研究需要决定入口，不要求先扫 Workspace 再遍历 RG。

## AnswerContract：本轮选择承担什么

obligations 是本轮选择交给判断、安排与复盘的责任，可以是取得认识、开展研究或交付产物；选择这些责任不等于保证全部当轮完成。履行可以得到正、负或不确定结果，本轮责任也不等于整个 Question 的完成标准。草稿可随着认识、重心、投入和有效指导修订，说明重要变化并重审受影响的覆盖与 Brief；正式接纳的 Plan 保持当时语义。不得为通过交接而隐藏未完成责任、抹去失败或降低支持标准，将尚未履行的责任改成 covered。

Plan 结合本轮重心、实际材料、资源和发现细化或调整投入，并把必要理由留在交接 notes；重心与稳定 Question、有效条件及已接纳责任的边界沿共享语义。

```text
AnswerContract
  source_question_ref
  source_idea_set_ref
  obligations[1..N]
    obligation_key
    statement
    minimum_support
    question_trace[] = unknown_statement | answer_shape | applicability_scope
    idea_relevance[实际有关的候选]
      idea_ref
      role = query_lens | experiment_lens | not_relevant
      rationale
  answer_contract_hash  # adapter 派生
```

每项义务说明与 Question 的实际联系；`idea_ref` 使用当前绑定 IdeaSet 的准确 `candidate_key`，说明候选对本轮工作的影响，无关候选无需逐项列出。`minimum_support` 说明实际履行该责任所需的观察、支持、适用边界或产物要求，不保证假设成立。数据准备、阅读、方法复核或负结果调查均可形成真实责任，局部开工条件仅约束实际依赖它的工作。

形成计划的依据是当前取舍能否成立：相关已有成果已被合理考虑，实际支持的关键正文可读且适用，未解决的信息问题不会被隐去。继续调查由可能改变取舍的冲突、缺口或遗漏驱动，不以搜索次数、全库遍历、读取证明或所有未知都消失为完成条件。未命中与分页未完、没有记录、权限不足、原件不可读分别处理，具体结果合同见 Owner 操作。

## 实际采用来源与覆盖

`selected_evidence_bindings` 表示**Plan 作出本次判断时实际采用的准确来源集合**，可供 EvidenceUse 说明科学支持，也可供 Brief 引用计划复用的材料；两种用途共用来源身份，不再区分首轮目录内来源与后来追加来源。这份历史采用记录不要求后续阶段把所有条目都用作执行输入。具体形状和来源类别见[Owner 操作](owner-operations.md)。发现结果提供 reader 与真实来源，Agent 说明科学用途，adapter／Owner 封装来源核验；目录大小与一次查询页长不限定可采用来源范围。材料被 Brief 选作复用输入，本身不表示它支持 covered。

正式来源可以是有测量或无测量的真实 Target 工作产物、HumanInput、ScientificOutcome、AssetVersion、LiteratureSnapshot 等已支持类别。保留实际生产者、精确版本及接纳事实；来源种类不预设科学价值。目录卡片、路径、URL、摘要和排序供导航，不能替代所采用主张所需的正文与来源。

```text
EvidenceUse
  obligation_key
  evidence_ref          # 引用 selected_evidence_bindings 中的准确来源
  supported_claim
  support_boundary
  contributing_idea_refs[]

CoverageDecision
  obligation_key
  disposition = covered | gap
  evidence_uses[]
  insufficiency | null
```

覆盖解释本次提交的同一份 AnswerContract：每项义务有一个判断，`covered` 有实际 EvidenceUse 并说明已足够履行什么责任，`gap` 说明相对义务仍缺什么并保留实际已有支持。覆盖判断与本轮安排分别表达；有 gap 不等于必须本轮开展补充工作，有 Brief 也不保证填平其关联 gap。草稿义务变化时同步重审覆盖和 Brief。负结果、无定论或没有新发现是否已履责，取决于实际责任与依据；一次搜索无结果或技术访问失败不能代替有充分支持的判断。

材料、授权或答复仍是作出当前规划判断的必要条件时，先解决具体缺口或通过 HC 求助；可独立继续的工作仍可推进。无可独立继续的工作且成功发起 HumanRequest 后，结束本次执行并交回系统，不自行轮询；草稿与已有认识留在原 Session／Workspace，正式答复后由系统 resume 原根。该等待不关闭原 Session、不完成 Plan，也不提交不完整 FormalPlan 或 StageCommit；其他独立根可继续工作，无需另建恢复包。外部条件暂不可得，但已有依据足以作出本轮停止、转向或保留未知的规划判断时，可按下述未安排 gap 规则交接。已存在的 HumanRequest、在途承诺与有效人类条件仍沿其真实生命周期处理，不因不安排工作而静默关闭或绕过。没有历史记录本身不阻止从当前问题和构想开展研究。

## Brief、后继和最终交接

每个 Brief 有 Plan 内唯一 `experiment_key`、非空、合法且在该 Brief 内不重复的关联 gap obligation keys、goal、characteristics、boundary constraints、semantic delta 和实际 Idea 来源。说明要取得什么观察或产物、为什么值得做、已有工作还缺什么及何时需要重新判断。这里的 ExperimentBrief 也可组织阅读、分析、数据准备、负结果调查和写作；不要求每项工作产生新测量或正结果。Brief 说明其实际推进范围，可以只处理关联 gap 的一部分。已有 Brief 关联不代表全部工作已承接；重要但未安排的剩余部分及取舍仍保留在既有不足说明或 notes。

沿用现有科学字段：`gap_obligation_keys[]` 与 `contributing_idea_refs[]` 是准确引用列表；`goal`、`characteristics`、`boundary_constraints`、`semantic_delta` 是正文字符串。`notes` 也使用正文字符串。统一采用字段不改变这些科学字段的含义。

Brief 可选的 `reuse_material_refs[]` 引用统一 `selected_evidence_bindings` 中计划复用的脚本、数据或其他材料的 `evidence_ref`，让 Bundle 沿准确来源判断和选择实施输入；没有选定复用材料时省略或为空。普通复用意图允许后继按实际需要选择、修复或替换材料，不要求逐项必用。这是交接材料入口，不是读取证明或新的强制清单，不能替代 EvidenceUse 的支持主张和边界。

明确写定的研究目标、比较条件及 held-fixed 精确内容仍按科学承诺处理；改变这类条件时走既有语义修订或 Reasoning 后继路径。`characteristics` 中的科学特征、拟取得观察和实现建议按正文实际含义理解，不能把全部描述默认成实现硬锁。普通实现取舍在实际工作交接中说明必要来路即可，不另设普遍偏离表。

Plan 为本轮值得推进的工作安排有意义的 Brief；同一 Brief 可关联多个 gap，同一 gap 也可由多份 Brief 共同推进，不要求全部 gap 都被 Brief 覆盖。Bundle 可以安排多个 Target 开展一份 Brief，不预定 Brief 与 Target 固定一对一。covered 不生成重复工作；有理由的复核应说明当前义务所需的新增核实及其缺口。未选入本轮的其他未知放在必要 notes 中，不机械扩充 obligations。Bundle／Target 自主决定具体实现、实际方法归属、执行依赖、顺序与补充核验。

未关联任何 Brief 的 gap，必须在其既有 `CoverageDecision.insufficiency` 中同时说明尚未履行的部分、为何本轮不安排，以及该判断的实际依据和限制；相关接续条件与问题可写入既有 notes。取舍可以涉及前提、适用范围、当前能力、本轮重心、价值及投入优先级；可构造工作不等于值得本轮投入，但困难、耗时或一次未命中本身不足以支持放下。有效指导和交付要求仍保留，暂不安排不等于取消要求或宣布完成。缺少充分判断时继续调查或修订，不以一句“不值得”完成规划，也不要求穷尽搜索或证明所有路线不可行。

部分 gap 有 Brief、部分未安排时，Bundle 执行已列工作，未安排责任与理由完整交给后继。已安排工作的真实前置条件仍须妥善处理，不能把必需条件标成未安排 gap 来掩盖依赖。

| 本轮判断 | 交接 |
| --- | --- |
| 全部义务已有足够支持，且 Brief 为空 | `no_new_experiment_required`，交出准确来源、支持与边界；AE 核验后跳过 Bundle，Reasoning 继续综合。 |
| Brief 非空，包括仍有未安排 gap 的情况 | `experiments_required`，交出已安排 Brief、真实覆盖及未安排 gap 的不足与理由；Bundle 仅组织已列工作。 |
| Brief 为空，仍有 gap，但已形成有依据的本轮不安排判断 | `no_new_experiment_required`，Plan 完成的是规划责任；AE 核验后跳过 Bundle，Reasoning 接收实际未履行责任、依据和未决问题。 |
| 关键原件、权限或答复仍是当前规划判断的必要条件 | 保留已完成工作及真实阻塞；无独立可做工作且 HumanRequest 成功后结束本次执行并交回系统等待，正式答复后恢复原根。具备判断条件后再提交适用 Plan，等待不是第三种 coverage 或 Plan 完成。 |

`experiment_briefs` 允许 `[]`。在 Plan 已完成上述规划责任并正式接纳的前提下，`bundle_disposition` 由 Brief 是否为空派生：

```text
experiment_briefs 非空 → experiments_required
experiment_briefs=[] → no_new_experiment_required
```

沿用字段名称；`no_new_experiment_required` 只表示本轮不进入 Bundle，不表示没有科学缺口、以后无需研究或 Question 已完成。未安排 gap 的键可由 `gap_set − union(Brief.gap_obligation_keys)` 派生为阅读视图，不新增存储字段、第三种 coverage 或第二份权威清单。取消全 gap 的 Brief 集合闭合约束后，系统不再以该约束机械防漏；理由是否充分、是否存在遗漏由根及既有独立审阅判断，不能声称提供等效的机械保证。

Idea 的 `NoViableCandidate` 和 Reasoning 正式选择的后继入口按 [Cycle 合同](../../references/cycle-work.md)处理，可能直接进入 Reasoning；它们不是已运行 Plan 可以自行冒用的出口。Plan 没有独立的 NoViable 出口。Plan 根据读到的真实依据调整工作；需要重做上游构想或改变已承诺核心科学语义时，按目标演化及后继职责交 Reasoning 安排。

最终科学输出包含 `answer_contract`、`coverage`、`experiment_briefs`、`selected_evidence_bindings` 和必要 `notes`。notes 保留重要取舍、未决问题、复核理由与后继需要的限制和接续条件，不要求重复全部调查过程。provider 返回 adapter 处理前的科学提交，正式存储按以下方式投影。

adapter 将提交的 `selected_evidence_bindings` **整组原样**放入正式 Plan 的 `source_bindings.selected_evidence_bindings`，同时保留 EvidenceUse 与 Brief 复用材料的来源；Question、IdeaSet 等其他绑定沿既有职责处理。这是提交来源在正式文档中的存储投影，不是新的来源权威。Bundle 沿 `reuse_material_refs` 在该列表按 `evidence_ref` 取得准确 reader，选择并绑定实际实施输入；后续选择、修复或替换不改写 Plan 当时采用的来源记录。

`evidence_reuse_set` 仅从 EvidenceUse 派生，不用它重建或裁剪完整的 `source_bindings.selected_evidence_bindings`，因此仅供实施的材料也能读回。其余派生包括 `gap_set`、`idea_trace`、`bundle_disposition` 和合同 hash；字段派生服务于接纳与读回，不另加 Agent 证明材料或另一套来源列表。

## 写作与产物交付

研究认识与产物交付可以合在一项 obligation，也可按实际责任拆开；不强制增加类型或交付字段。既有 `statement` 与 `minimum_support` 说明目的、内容、范围和任务要求。科学支持已经充分但要求的文稿尚未完成时，该交付仍为 gap：EvidenceUse 如实保留已有支持，`insufficiency` 说明欠缺的组织、写作、修订或文件交付。值得本轮推进的写作或部分交付形成 Brief；否则沿同一未安排 gap 规则保留责任和理由。

区分两个时点：Plan 提交时已经存在且符合要求的准确产物，可以沿既有来源合同支持 covered；未来产物不能预先写入该 Plan 的采用来源快照。Plan 之后产生的文稿或其他产物，由 Target 实际完成并经既有 Owner 接纳，由 Bundle 报告实际履责，再由 Reasoning 综合。后续完成不追写旧 Plan 的 coverage，也不强制再走一轮 Plan 或来源回流。

完成依据适应实际任务：提纲、修订稿与最终稿分别按各自约定交付，阅读、推导、定性分析和写作不套用实验达标标准。编译、渲染、文件格式等仅在任务需要时成为交付要求；独立审阅沿原任务与 Writing 规范，不新增通用审批门槛。

## 独立审阅、正式接纳与续接

主根在连续工作中委派独立子 Agent 取得实质反馈，并自行核实、取舍与修订完整草稿。审阅关注科学责任、已有成果、支持边界、已安排工作与未安排缺口的取舍，质疑遗漏和过早放弃，不靠固定审查表单或 reviewer 批准控制接纳。根可以让子 Agent 直接使用已授权入口，不要求根先复制全部材料。正式接纳前发现遗漏，由根修订；接纳后发现的问题由 Reasoning 如实综合并安排后继纠正，不倒改已接纳 Plan。

RM 保存准确 PlanDocument，RG 依据结构、来源、身份、权限和正式事实接纳 FormalPlan，AR／AE 处理真实运行与 StageCommit。科学充分性、工作价值及不安排理由由根判断；Owner 不审批科学取舍，`insufficiency` 非空也不等于论证充分。RM 已保存而 RG 拒绝时，保留旧内容并在原根依具体反馈修订为新内容身份；未知提交结果先核对原操作，不重复创建。精确操作结果、工具错误和恢复方式见 Owner 操作。

新指导、正式答复、新材料、拒绝反馈及其他实际续接回到原根，业务 user 保留同一请求绑定的 Cycle／Plan／focus 短定位，再追加必要变化并重审受影响部分；不因此重发全部资源。上下文压缩或中断后，从准确正文与运行事实补回需要的内容，保留仍适用的科学认识。已接纳 Plan 保持原语义；新目标、授权或上游工作变化按既有正式机制处理，不静默改写原输入。系统内部协调尽量封装，科学充分性、取舍和修订留给 Agent。

## 能力边界

文件保管与研究历史发现沿[工具合同](tool-contracts.md)使用，工作目录按宿主给出的实际路径和读写范围访问，生命周期沿[共享收尾约定](../../references/materials-and-custody.md#交接与工作区收尾)。缺少关键能力时说明具体受阻步骤，不能据此声称没有材料；路径可达、正式接纳与科学判断各保留自身依据。
