# Idea 候选与闭包契约

## 研究结果

恰好形成 `IdeaSet | NoViableCandidate` 一项。`IdeaCandidate` 有唯一 `candidate_key`、`direction`、`rationale`、`evidence_boundary`；后者区分已接纳证据、推断和未知。假设、风险、核验方式和候选区别在 `notes` 按需表达，不套所有研究都必须填满的模板。

`IdeaSet` 含一个或多个实质不同候选；可给 `binding=false` 的 recommendation，说明比较和建议理由，完整集合仍交给 Plan，由 Plan 正式选择。当前输出是一份集合，其他历史集合及正式沿用的来路按[输出与交接](io-contract.md#输出与交接)处理。`NoViableCandidate` 说明探索范围、考虑过的路线族、不可行理由、证据边界、推翻条件，以及当前为何不能负责地继续 Plan；判断仅限本次冻结范围。

## 修订与接纳

按[主 Skill 的独立审阅与交接](../SKILL.md#2-独立审阅与交接)在根内连续形成最终内容。草稿和最终内容的记录分别绑定实际 hash；独立反馈留在原生执行记录中，根负责最终判断，不要求审查表单、审阅者身份证明或批准记录。Owner 拒绝后的后继提交绑定原 submission、真实拒绝 receipt 和根的实质修订，形成不同结果 hash。

同一 submission identity 仅绑定一个不可变规范 payload 与调用闭包。RM 内容 checkpoint、RG 决策、AR 完成和 AE commit 各自持久保存。`rejected` 形成有来源的后继；`stale` 重验精确绑定；`needs_input` 等待精确依赖；`outcome_unknown` 先对账；`technical_blocker` 在副作用已明确后按正常路径恢复。

确需改变 Quest 整体目标或完成标准时，按[正式 Quest 目标演化](../../references/cycle-work.md#正式-quest-目标演化)作研究判断，结合当前完整目标、完成标准、有效持续条件和实际工作说明变化及依据。新目标与完成标准必须完整成立，未变条件和安排沿明确有效基准由平台继承；以正式结果确认生效，候选及后继工作的影响沿实际状态交接。

本阶段提交沿准确输入和实际执行记录解释；初始输入记录保持原样，后续指导、修改与撤回关联到实际工作，由原根继续处理。当前能否接纳或执行仍按当前事实与权限判断；切面过期时保留 stale 事实，沿已有职责取得有效绑定后继续，不改写已接纳 Idea 内容，也不因换取绑定而重建科学工作。终态、已撤销或失去执行资格的工作不能靠 resume 复活。目标演化不代替 Question 建题或后继选题；新问题线索与依据留给 Reasoning 综合判断。

## ExhaustionProposal

仅在所有待提交、已接纳未消费结果、人类待办、技术阻塞、未知结果和既有 StageCommit 均完成对账，且 `IdeaSet` 与 `NoViableCandidate` 都无法辩护时提出。次数、评分或单次失败不是耗尽依据。AE 独占是否形成 StageCommit 的判断；等待和技术问题不能改写为科学耗尽。
