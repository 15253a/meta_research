# Idea 候选与闭包契约

## 研究结果

恰好形成 `IdeaSet | NoViableCandidate` 一项。`IdeaCandidate` 有唯一 `candidate_key`、`direction`、`rationale`、`evidence_boundary`；后者区分已接纳证据、推断和未知。假设、风险、核验方式和候选区别在 `notes` 按需表达，不套所有研究都必须填满的模板。

`IdeaSet` 含一个或多个实质不同候选；可给 `binding=false` 的 recommendation，说明比较和选择理由，但正式选择由 Plan 负责。`NoViableCandidate` 说明探索范围、考虑过的路线族、不可行理由、证据边界、推翻条件，以及当前为何不能负责地继续 Plan；判断仅限本次冻结范围。

## 修订与接纳

按[主 Skill 的独立审阅与交接](../SKILL.md)形成最终内容。草稿和最终内容分别绑定实际 hash；Owner 拒绝后的后继提交绑定原 submission、真实拒绝 receipt 和根的实质修订，形成不同结果 hash。

同一 submission identity 仅绑定一个不可变规范 payload 与调用闭包。RM 内容 checkpoint、RG 决策、AR 完成和 AE commit 各自持久保存。`rejected` 形成有来源的后继；`stale` 重验精确绑定；`needs_input` 等待精确依赖；`outcome_unknown` 先对账；`technical_blocker` 在副作用已明确后按正常路径恢复。

## ExhaustionProposal

仅在所有待提交、已接纳未消费结果、人类待办、技术阻塞、未知结果和既有 StageCommit 均完成对账，且 `IdeaSet` 与 `NoViableCandidate` 都无法辩护时提出。次数、评分或单次失败不是耗尽依据。AE 独占是否形成 StageCommit 的判断；等待和技术问题不能改写为科学耗尽。
