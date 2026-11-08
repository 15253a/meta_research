# Reasoning 的 Owner 操作

当前 scoped 工具目录决定实际可用能力，输入已有内容不要求按固定顺序再读。按需要选择以下阶段入口及共享发现／reader：

工作资料的读取、加工和保管按[材料与保管](../../references/materials-and-custody.md)处理；研究积累及实际用途从 RG 对应入口发现，涉及人类协作时从 RG 的 Human Interact 用途找到 `human_request.read`，沿返回的精确 reader 读取历史指导与处理记录；当前指导与反馈由 HC 的 `human_guidance.read`／`human_guidance.feedback` 处理。当前根交接未读、待验证部分及影响；需要上游重做或继续投入时，由本 Reasoning 按现有后继入口决定，不从材料反馈直接创建 Cycle。

| 操作 | 用途 |
| --- | --- |
| `advancement_engine.reasoning_stage_run.observe` | 核验当前请求、epoch 和 AE 闭包。 |
| `research_memory.reasoning_evidence.read` | 按研究需要读取精确冻结证据与角色，包括已核验 Plan 复用来源；可用列表本身不表示实际采用。 |
| `research_graph.reasoning_context.read` | 读取本次冻结的已接纳 Question／Quest／Goal、历史身份与领域上下文，结合正文作独立研究判断；系统使用同一冻结版本装配元信息。 |
| `research_graph.questions.page` | 按需发现同 Quest 的问题、状态及准确正文／历史 reader；按返回范围和分页继续，帮助比较相关路线。 |
| `research_graph.question_history.read` | 按精确 Question 读取题目记录与已接纳综合历史，包括暂放题；沿返回的准确入口展开必要正文。 |
| `research_graph.question_relations.read` | 读取相关问题及已有横向关系说明，核实联系的依据和适用范围。 |
| `research_graph.quest_goal.read` | 读取当前 Quest Goal、持续条件、本逻辑操作冻结工作切面和指导对齐。 |
| `research_graph.quest_goal.evolve` / `.reconcile` | 真正改变整体目标或完成标准时，沿[正式目标演化流程](../../references/cycle-work.md)判断变化并形成完整新 Goal revision，未变项沿有效基准继承；未知效果对账原操作。 |
| `research_graph.target_formal_results.read` | 按精确 target_ref 展开 VariantRun、适用评价和 MetricResult，包括已交接但尚未评价的 Run；资源候选按[主 Skill 的整理步骤](../SKILL.md#交接前整理已接纳-target-资源)与共享登记流程使用。 |

本轮 `cycle_focus` 原文与来源随首轮直接提供；需要补读时沿返回的正式 transition 入口取得同一正文，读取、恢复及缺失处理遵循[NextCycleProposal 合同](contract.md#nextcycleproposal)。字段沿现行 transition 合同保存、读取与接续。

检索或复用历史时读[研究导航](../../references/research-navigation.md)，不把首批目录当作全集。人类输入与处理历史按当前获授的准确 reader 读取，按[人类协作指导](../../references/human-guidance.md)区分意见、授权、HumanRequest satisfied 与 Quest 完成确认。

## 上下文装配与准确读取

首轮输入直接提供冻结 Question 的六字段正文，以及有效 Goal、完成标准和持续条件；ContextPack 同时呈现 FormalPlan 及全部采用来源绑定、Bundle 最后非空已接纳策略 notes，以及每项 Target 的关键结果和限制。正文、身份和按需 reader 对应同一冻结来源，具体交付与续接规则见[上下文与准确材料](contract.md#上下文与准确材料)。按返回 reader 展开准确原文与分页，正式修订保留当前声明与原依据。

`cycle_focus` 已由 Reasoning 的输出 schema、原接纳及后继装配接通；正文随 transition 保存与读取，各根沿实际前驱或首轮来源取得同一重心。恢复仍在原根、原目录和准确冻结输入上进行。按[语义契约](contract.md)修正缺失或冲突，不能补造首轮前驱。

Target 文献整理继承共享材料保管与导航合同。DeepFetch → LiteratureSnapshot → QuestionLiteratureRevision 保持；普通文献沿 research_memory.literature.register 保存外部来源和已接纳原件 binding，再用 .reference 关联 Question、research_ref、用途和真实 research_notes binding。用 .read 查回记录、content.read 取得原件与分析，文献原作与本次阅读各自保留来源。未取得全文如实登记 missing／waiting_user，不虚构 DeepFetch Run 或专用 literature_candidates。接口不可用、部分失败和未完成按实际步骤交接，不当成无候选；空候选或无保留价值可不登记。

历史按 Question／Cycle 关系及准确 reader 追溯，工作目录按本次获授路径使用。需要保管选定材料时调用 `research_memory.assets.intake`，以返回的准确版本和 reader 接续；需要查明可用操作或参数时，沿[当前获授工具与准确入口](../../references/research-navigation.md#当前获授工具与准确入口)读取本根合同。权限不足、原件不可读或结果未知时，按实际失败与对账入口处理。

## 写入权限与资源登记

Reasoning 阶段专用写入是 `research_graph.question_relations.record`；共享 catalog 还授予 Dataset 的 register、register_version、reference、derive 和 Environment 的 register、reference 及各自 reconcile。按实际研究与当前 scope 使用；执行前核实 currentness，绑定数字材料时核实原件已正式接纳，未知效果用原 `effect_id` 对账。资源整理的完成条件和时序以[主 Skill 的交接前整理步骤](../SKILL.md)为准，阶段名称不额外增加等待 ScientificOutcome 的条件。

可委派明确范围的资源登记任务，根按主 Skill 的完成条件独立读回核验。数字材料沿用已有保管与版本，登记不代替正式执行输入绑定，也不形成新的科学测量。

## Owner 分工及恢复

AE 管阶段请求、epoch、StageCommit、后继 Cycle 和 Quest 结束；AR 管 Run／Attempt／Session／Fence、provider 操作、执行事实及恢复；RM 管不可变内容；RG 管科学接纳、QuestionAnchor、Goal 和领域事实；HC 管人类请求、Web preview 和明确完成确认。Agent 负责科学判断、唯一后继候选与修订，不签发 Owner 凭据。

provider 正常交接后，daemon 沿公开接口保存 AR 执行、RM 内容和 RG 决策，再由 AE 验证 current request／epoch 与完整 receipts 推进。各边界独立幂等；响应丢失先查询原身份，从首个未完成步骤继续。仅经公开接口核实，不读取数据库、spool、seal key 或私有状态机伪造接纳或恢复。

CandidateCompletion 还需当前 Web preview、用户明确确认、RG Goal／完成接纳和 AE 结束转换。整体指导尚未判断目标影响或已判确需演化时保持待对齐；局部指导不因力度5阻止整体完成。旧无范围确认的5档记录按原文及历史待对齐语义读回。真实 Goal 演化后，旧 CandidateCompletion 仅作历史可读，AE 以更高 epoch 签发新 Reasoning 请求重新综合当前 Goal。拒绝、stale、未知或未响应都保持实际状态，不结束 Quest。
