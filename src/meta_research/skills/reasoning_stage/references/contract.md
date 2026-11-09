# Reasoning 语义契约

## 调用闭包

`ReasoningSkillRequest` 绑定当前 StageRunRequest、Run、Attempt、Fence、根 Session、Cycle、Question、Quest、Goal revision、epoch、ContextPack ref／hash、冻结证据和运行绑定。ContextPack 包含已接纳 Question、文献版本或 none、Idea→Plan→Bundle StageCommit、Plan 证据或 none、TargetCommit 及研究上下文。

每次业务 user 开头按[本次阶段定位](../../references/cycle-work.md#本次阶段定位)直接说明请求绑定的 Cycle 身份／轮次、Reasoning 阶段及本轮重心；同根续轮与恢复保持原绑定。

本轮重心按[共享语义](../../references/cycle-work.md#本轮重心)直接提供原文与准确来源，包括直接进入 Reasoning 的后继 Cycle。完整 Question 保留既有 `title`、`unknown_statement`、`answer_shape`、`applicability_scope`、`background_context`、`requirements_constraints` 六字段，按[共同语义](../../references/cycle-work.md#新问题与后继-cycle)保留四项核心与两项按需可空补充；`cycle_focus` 不替代其中任何字段。首轮与缺失来源按下文处理。

Plan 复用保持精确完整 FormalPlan，包括 obligations、coverage、Brief、各项 insufficiency 和 notes；直接提供 `FormalPlan.source_bindings.selected_evidence_bindings` 的完整准确来源集合，包括科学支持和 `ExperimentBrief.reuse_material_refs[]` 使用的材料。`evidence_reuse_set` 仅由 `EvidenceUse` 派生，不能裁剪或替代完整集合。需要正文时沿绑定的 `evidence_ref` 读取；集合可达不要求通读未被当前判断或工作使用的材料。各 `EvidenceReuseLeaf/v1` 绑定 catalog entry／use hashes 与真实来源：测量叶保留实际 MetricResult、EvaluationAttempt、Run／Commit 及 RM／RG receipts；无评价 WorkProduct 保留真实 Run／Commit，不补 EvaluationAttempt；HumanInput、ScientificOutcome、AssetVersion、LiteratureSnapshot 使用各自核实的 `source_binding`，不强加 Target 测量身份。

Target 结果可以没有总分类。沿精确结果版本、真实指标、评价或未评价状态、缺测说明、技术失败及研究记录独立综合科学支持与不确定性；历史结果的描述性分类只是原文材料。冻结来源说明可用依据，实际采用与用途以相应研究记录为准。本阶段 ScientificOutcome 继续表达自身的科学判断与边界。

adapter 先核验静态 identity／hash／closure，provider 通过获授 scoped MCP 核实当前 AR／AE 范围。未知 currentness 不能当作 current。

Bundle 因 Brief 为空而跳过时，完整 Plan 仍是本轮输入；跳过可以来自全部责任已有支持，也可以来自有依据的本轮不安排判断，不据此认定 gap 已履行。部分工作已开展、部分 gap 未安排时，将实际结果与完整 Plan 的未履行责任、不安排理由和相关历史一并综合，解释它们对继续、放下或重审的影响。可以按研究意义分组，不强制逐 gap 填写固定选择表。Plan 的规划判断本身可以有研究价值，同时保留未解决部分及判断限制。正式接纳后发现规划遗漏或取舍有误时，如实综合并安排后继纠正；Reasoning 不成为 Plan 的倒序接纳者，也不追改旧 Plan。后续 Target 已完成的写作等产物按实际接纳结果评价，不追写旧 Plan 的覆盖或采用来源快照。

## 上下文与准确材料

同根研究过程：认真分析本 Cycle，经 RG 放回整体路线作抽象综合，判断关键处并选择下一步。过程以[主 Skill](../SKILL.md)为准，现有输入、科学综合与 notes 承载所需材料和判断；不新增阶段、全局快照或输出字段。每轮承担全局审视，准确读取按需展开并允许回看。

首轮资料取舍：完整 Plan／Bundle 交接及各项 Target 的关键结果与限制直接给出，其余准确原文按需读取。需要补读或执行 Owner 操作时，按[Owner 操作](owner-operations.md)选择当前获授入口。人类协作历史沿[Human Interact](../../references/research-navigation.md#human-interact理解人类协作及其研究影响)的原文与处理 reader 按需读取，保留各根反馈的准确版本及当时目标判断，不用当前根处理摘要覆盖其他历史。首轮按科学用途直接组织：

- 本次 Cycle／阶段／重心与来源、请求绑定，完整 Question、当前有效 Goal 与持续条件，以及适用的人类指导。保留本逻辑操作的冻结工作切面；后到指导与当前 Goal 的差异据实说明。
- 当前已接纳的完整 FormalPlan 科学正文、完整采用来源集合，及已接纳 BundleReport、各 Target 真实结果和完整 Bundle 策略交接 notes。阶段合法跳过时显示原因和精确依据，Bundle 无 Brief 跳过仍完整保留 Plan 的实际 gap 及不安排理由；缺失应有材料时显示实际阻塞。未产生的 Plan、Bundle 或 Target 不以旧轮对象填补。
- 本轮每项实际工作的用途、真实执行／评价状态、关键具名结果、单位与比较对象（适用时）、缺测或失败原因、影响解释的限制，以及准确 TargetCommit／结果／说明入口。含无评价工作与负结果；长说明分段保留完整可读入口，不能把任何一项工作或决定性限制裁成不可见。它们是既有结果与研究说明的阅读呈现，不新增结果卡存储或统一指标清单。
- 真实前驱必要交接、当前 Question 已有认识及相关历史的准确入口与分页范围。首批历史说明其范围，不能冒充全部历史；原 IdeaSet 及更早材料按当前判断需要展开。

Question 正文沿本请求冻结的 `AcceptedQuestionBinding.content_ref`／`content_hash` 取得，六字段原样交付，允许为空的两项补充仍保留为空字符串；`research_memory.stage_context.read` 的 `source=question` 返回同一准确内容。请求冻结的 Goal revision 与完成标准完整保留；本次 operation 冻结指导中的当前有效 Goal 和持续条件也完整直给，各自保留实际来源，持续条件可在同一 Goal revision 下更新。这些必要正文不受普通辅助 context 的字节、深度或列表预览限制。较长历史和辅助材料仍按需展开；原根续接、拒绝修订与恢复沿原冻结请求及必要增量继续，不以重复全文或查询 latest 替换原依据。

审视其他路线时，通过获授 RG Question 发现入口找到同 Quest 的相关或暂放问题、准确正文及历史，再按需要展开关系和依据。工具可发现的材料不表示已经读取或采用；不把所有问题和历史全文追加为首轮必给内容。

“完整正文”指 Question、有效 Goal／指导、FormalPlan 科学正文和 Bundle 最终 notes；“完整来源集合”指全部采用绑定及其准确元信息，不把每份被引用材料的全文一并注入；各项实际工作的关键结果与限制直接呈现，逐样本数据和长日志按需读取。影响判断的实际限制须首轮可见，逐样本材料保留准确入口。

Bundle 策略 notes 取沿本次 TargetPlan／StrategyUpdate 接续的最后一条非空且已接纳记录，正常最终汇总来自 StrategyUpdate.notes，保留对应 proposal ref／hash 及 StageCommit 来源。完整 notes 已包含前序仍有效的线索、证据边界和零个或一个新 Question 推荐；其余线索是保留项。Reasoning 核对其依据并独立决定是否建题、选题与下一轮 focus，不把推荐解读另存为第二份权威。BundleReport 的工程状态与责任覆盖不代替科学判断，精确报告与 notes 各自保持来源。

当前输入已提供准确完整正文时可直接使用。形成比较、因果解释、登记、改向、建题或完成判断前，展开承担该判断的准确原文：包含关键限制的 Target 研究说明与实际评价；采用材料的具体版本与适用协议；相关历史的原生产轮次；正式修订的当前声明、superseded_claims、原闭包及其依据。展开深度由判断需要决定，不要求每次读取全量逐样本数据、日志或所有冻结材料，也不增加读取回执。

本轮正常同根工作中的阅读、委派、审阅和修订连续进行。只有新材料、正式修订、指导答复或恢复需要时追加业务 user，每次保留绑定的 Cycle／Reasoning／focus 定位，说明变化、准确来源和要重新判断的内容；既有指令、tools、权限、schema 与有效历史继续由 Harness 维持。正式修订保留原依据与发生顺序；替换 epoch 使用新请求的完整绑定，不能沿旧请求提交。

## 封闭输出

顶层恰含：

```text
schema_ref = meta-research/reasoning-stage-output/v1
scientific_outcome: ScientificOutcomeCandidate
next_cycle_proposal: NextCycleProposal | null
candidate_completion: CandidateCompletion | null
```

后两项恰好一项非 null。科学结果与 transition 绑定同一 request、Cycle、Question、Quest、epoch 和 outcome identity，`is_authoritative=false`。

每项科学引用恰含 `kind + ref + finding`，其中 `finding` 使用 `supporting | negative | partial | context` 枚举，具体支持范围和理由写入 claim、scope、causal、synthesis 或 notes。引用冻结闭包、Plan 精确复用或同 Quest 可核验历史。Owner 逐条检查精确版本、范围和来源；引用类别、数量和是否测量不预设科研资格。纯理论可零外部引用，仍说明推导和局限。disposition 对 claim、missing 和 uncertainty 的具体形状由当前 `reasoning_contract.py` 校验。

本次冻结 `research_context/v3` 的可用来源与已知历史身份由系统装配。模型输出的 `causal_interpretation` 只写 `attribution_basis_refs`、`claim_scope`、`statement`、`sufficiency_rationale`、`confounders`；`research_synthesis.current_question` 只写 `question_ref` 与 `progress`。四组冻结因果来源数组和 `prior_accepted_outcome_refs` 在模型完成后、hash 与接纳前由系统绑定同一冻结版本，随正式内容保存并供后继读取。可用来源不会因此成为已阅读、已采用或已支持某结论的证据。

`evidence` 和 `attribution_basis_refs` 由 Agent 选择实际采用的精确依据。以字符串 ref 为元素的引用数组每项放一个完整 ref，多个来源各列一项；解释写入 statement、support_scope、notes 等正文。例如冻结来源含两个 TargetCommit，但只有一份文献与本次结论相关时，选择该文献作为 evidence／attribution basis，分别解释支持范围和因果局限；系统仍保存两个 TargetCommit 的可用来源绑定。

每个冻结父 Question 的 `impact` 与 `statement` 仍由 Agent 独立判断，保持 `question_ref` 将判断明确关联到其对象，系统再按冻结父链顺序整理。这个引用承载判断对象的语义，不能从无对象的正文推定。系统携带当前 Question 的已知历史 outcome 身份，认识变化由 `progress` 表达。错误来源、错版本和冲突身份会指出具体对象并拒绝，沿反馈修订真实声明。

当前执行与恢复使用 `meta-research/reasoning-research-context/v3` 冻结输入和本次真实操作记录。历史操作及其原契约仅作只读证据，恢复沿原 request／operation 对账，保持当时的来源与版本。

`support_scope`、`limitations`、`causal_interpretation` 和四尺度 `research_synthesis` 表达认识和继续／改变／等待理由，允许认识未变。AE 冻结 graph revision、活动问题、父链、当前 Question 历史页、Goal revision、上游 Commit 和 Target 来源；有界历史页含总量与继续入口，不代表完整历史。正文按当前冻结版本核验，恢复不切换 latest。

`disposition` 对应本次明确范围的 `claim`；相关局部事实、其他未决部分和对整个 Question 的影响分别留在 support_scope、limitations、research_synthesis 与 notes，不因一个限定主张有答案就宣称整个问题已解决。四尺度的 cycle.impact、current_question.progress、每个父问题的 impact／statement 和 quest.impact 都由 Agent 判断；系统补入上文指定的冻结来源数组和历史身份，不能代替这些科学判断。

## NextCycleProposal

现行合同字段 `NextCycleProposal.cycle_focus: string`，新提交必填且非空，承载下一轮重点做什么；粒度及与 Idea、Plan 的分工见[本轮重心](../../references/cycle-work.md#本轮重心)。CandidateCompletion 不含此字段。其余 transition 身份、接纳及 skip 规则保持既有合同，生产、读取和装配沿下述正式入口。

`entry_stage=idea | plan | reasoning`。`typed_skip_basis_refs_by_stage` 恰覆盖入口前的阶段，每项为非空去重依据：idea 无 skip；plan 用已接纳 IdeaSet 覆盖 Idea；reasoning 的 Idea、Plan、Bundle 三项均为 `[source ScientificOutcome outcome_ref]`，由 AE 形成 absent-input skip Commit，不复用旧 Plan／Bundle。Bundle 不是合法后继入口。

RG 在 transition 接纳时重验 QuestionAnchor、present／open 和各依据。ScientificOutcome 只支持上述 absent-input skip，不替代目标 Question 的 IdeaSet 或 FormalPlan。自主创建取得新信息后可重新决定目标、入口及 skip，仍按实际已接纳资产验证。

这里的 source ScientificOutcome 是与该后继提案共同提交的本次综合结果；普通路径沿既有完整内容校验科学结果和 transition 并接纳，不新增两次独立接纳步骤。它在下一 Cycle 成为真实前驱来源，不能误用更早一轮 outcome 或补造首轮历史；自主建题的已有 checkpoint／接纳分段按专节执行。

### 读取、装配与恢复

`cycle_focus` 随完整 NextCycleProposal 一起参与既有内容 hash、接纳、保存和公开投影；来源复用该 transition 的 ref／hash、实际前驱 Cycle 与 StageCommit，不新增 focus 对象、ref 或版本。精确读取返回同一已接纳 transition 的原文和来源，公开 NextCycleProposal 投影保持该字段，不以 ScientificOutcome 摘要或 IdeaSet 生产轮次代替。

AE 沿正式后继关系取得该正文，由各根输入装配直接提供：Idea、Plan、Bundle、Target 及 Reasoning 都保留本 Cycle 的同源原文。正常 Idea 入口之后的 Plan 仍接收，跳过 Idea 和直接 Reasoning 入口也接收；Target 放入任务说明，不扩展冻结科学合同。已知正文与准确来源足够时可直接使用，不为证明读取再调用 reader。

同一 Session 的恢复继承已有正文；必要重装配沿同一冻结 transition 读取，不切换 latest、不重算重心。首轮使用创建／启动来源，或首个根写入既有 notes 的重心，并按原阶段交接继续；不存在前驱 transition 时不补造一个。

新提交缺少或空置 `cycle_focus` 时由原 Reasoning 根修订完整提案；本版正常后继已有正式来源而本次首回合输入装配缺失时，属于技术问题，沿准确来源补取或报告具体阻塞，不能自动发明重心。历史只读内容确无该字段时如实显示缺失，不补写旧内容，也不据此新增旧系统任务的兼容执行分支。读取失败、权限不足与原文确实不存在分别返回实际原因，按既有恢复或求助入口接续。

## 独立审阅与技术边界

审阅按[主 Skill](../SKILL.md)执行。权限以当前 catalog 及配对 reconcile 为准，primary／review 名称不额外收窄已授 Dataset／Environment 效果。缺 scoped authority／endpoint／credential、必要操作或相应绑定时，在相应执行前保留技术阻塞；内容 hash、source binding、封闭 schema 或 native Session 不一致时不形成有效交接。未知 provider 结果对账原 operation，不重放。

技术问题不改写为 `insufficient_evidence`；该 disposition 只描述来源可核验时科学支持仍不足。

## AutonomousCreation 与恢复

初始 AR checkpoint 保存执行草稿和 hash；正式建题以 DeepFetch summary 接纳及同一 native Session 读回后的 create 决定为前提。Session 根据摘要中的研究现状、已有题覆盖范围和独立研究价值调整候选并决定 create／decline；摘要接纳本身不要求创建问题。failed／cancelled 无 summary 时还可 retry。create 修订后才接纳唯一 ScientificOutcome，再经 HC／RM／AE／RG 创建 Question 并挂文献；decline 直接完成普通综合。最终同一 Session 可选择当前、已有或新 Question。原 checkpoint、科学内容、DeepFetch 尝试及决定不可变，重启按已有事实继续，不重复 summary、候选或 Question。
