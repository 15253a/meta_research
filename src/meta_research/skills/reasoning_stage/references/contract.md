# Reasoning 语义契约

## 调用闭包

`ReasoningSkillRequest` 绑定当前 StageRunRequest、Run、Attempt、Fence、根 Session、Cycle、Question、Quest、Goal revision、epoch、ContextPack ref／hash、冻结证据和运行绑定。ContextPack 包含已接纳 Question、文献版本或 none、Idea→Plan→Bundle StageCommit、Plan 证据或 none、TargetCommit 及研究上下文。

Plan 复用保持精确 FormalPlan 和原样 `evidence_reuse_set`，各 `EvidenceReuseLeaf/v1` 绑定 catalog entry／use hashes 与真实来源：测量叶保留实际 MetricResult、EvaluationAttempt、Run／Commit 及 RM／RG receipts；无评价 WorkProduct 保留真实 Run／Commit，不补 EvaluationAttempt；HumanInput、ScientificOutcome、AssetVersion、LiteratureSnapshot 使用各自核实的 `source_binding`，不强加 Target 测量身份。

Target 结果可以没有总分类。沿精确结果版本、真实指标、评价或未评价状态、缺测说明、技术失败及研究记录独立综合科学支持与不确定性；历史结果的描述性分类只是原文材料。冻结来源说明可用依据，实际采用与用途以相应研究记录为准。本阶段 ScientificOutcome 继续表达自身的科学判断与边界。

adapter 先核验静态 identity／hash／closure，provider 通过获授 scoped MCP 核实当前 AR／AE 范围。未知 currentness 不能当作 current。

## 封闭输出

顶层恰含：

```text
schema_ref = meta-research/reasoning-stage-output/v1
scientific_outcome: ScientificOutcomeCandidate
next_cycle_proposal: NextCycleProposal | null
candidate_completion: CandidateCompletion | null
```

后两项恰好一项非 null。科学结果与 transition 绑定同一 request、Cycle、Question、Quest、epoch 和 outcome identity，`is_authoritative=false`。

每项科学引用恰含 `kind + ref + finding`，引用冻结闭包、Plan 精确复用或同 Quest 可核验历史。Owner 逐条检查精确版本、范围和来源；引用类别、数量和是否测量不预设科研资格。纯理论可零外部引用，仍说明推导和局限。disposition 对 claim、missing 和 uncertainty 的具体形状由当前 `reasoning_contract.py` 校验。

本次冻结 `research_context/v3` 的可用来源与已知历史身份由系统装配。模型输出的 `causal_interpretation` 只写 `attribution_basis_refs`、`claim_scope`、`statement`、`sufficiency_rationale`、`confounders`；`research_synthesis.current_question` 只写 `question_ref` 与 `progress`。四组冻结因果来源数组和 `prior_accepted_outcome_refs` 在模型完成后、hash 与接纳前由系统绑定同一冻结版本，随正式内容保存并供后继读取。可用来源不会因此成为已阅读、已采用或已支持某结论的证据。

`evidence` 和 `attribution_basis_refs` 由 Agent 选择实际采用的精确依据。以字符串 ref 为元素的引用数组每项放一个完整 ref，多个来源各列一项；解释写入 statement、finding 等正文。例如冻结来源含两个 TargetCommit，但只有一份文献与本次结论相关时，选择该文献作为 evidence／attribution basis，分别解释支持范围和因果局限；系统仍保存两个 TargetCommit 的可用来源绑定。

每个冻结父 Question 的 `impact` 与 `statement` 仍由 Agent 独立判断，保持 `question_ref` 将判断明确关联到其对象，系统再按冻结父链顺序整理。这个引用承载判断对象的语义，不能从无对象的正文推定。系统携带当前 Question 的已知历史 outcome 身份，认识变化由 `progress` 表达。错误来源、错版本和冲突身份会指出具体对象并拒绝，沿反馈修订真实声明。

恢复冻结 `research_context/v2` 的历史操作时，使用[历史输出契约](legacy-v2-contract.md)；既有内容与 hash 保持原契约。

`support_scope`、`limitations`、`causal_interpretation` 和四尺度 `research_synthesis` 表达认识和继续／改变／等待理由，允许认识未变。AE 冻结 graph revision、活动问题、父链、当前 Question 历史页、Goal revision、上游 Commit 和 Target 来源；有界历史页含总量与继续入口，不代表完整历史。正文按当前冻结版本核验，恢复不切换 latest。

## NextCycleProposal

`entry_stage=idea | plan | reasoning`。`typed_skip_basis_refs_by_stage` 恰覆盖入口前的阶段，每项为非空去重依据：idea 无 skip；plan 用已接纳 IdeaSet 覆盖 Idea；reasoning 的 Idea、Plan、Bundle 三项均为 `[source ScientificOutcome outcome_ref]`，由 AE 形成 absent-input skip Commit，不复用旧 Plan／Bundle。Bundle 不是合法后继入口。

RG 在 transition 接纳时重验 QuestionAnchor、present／open 和各依据。ScientificOutcome 只支持上述 absent-input skip，不替代目标 Question 的 IdeaSet 或 FormalPlan。自主创建取得新信息后可重新决定目标、入口及 skip，仍按实际已接纳资产验证。

## 独立审阅与技术边界

审阅按[主 Skill](../SKILL.md)执行。权限以当前 catalog 及配对 reconcile 为准，primary／review 名称不额外收窄已授 Dataset／Environment 效果。缺 scoped authority／endpoint／credential、必要操作或相应绑定时，在相应执行前保留技术阻塞；内容 hash、source binding、封闭 schema 或 native Session 不一致时不形成有效交接。未知 provider 结果对账原 operation，不重放。

技术问题不改写为 `insufficient_evidence`；该 disposition 只描述来源可核验时科学支持仍不足。

## AutonomousCreation 与恢复

初始 AR checkpoint 保存执行草稿和 hash；正式建题以 DeepFetch summary 接纳及同一 native Session 读回后的 create 决定为前提。Session 根据摘要中的研究现状、已有题覆盖范围和独立研究价值调整候选并决定 create／decline；摘要接纳本身不要求创建问题。failed／cancelled 无 summary 时还可 retry。create 修订后才接纳唯一 ScientificOutcome，再经 HC／RM／AE／RG 创建 Question 并挂文献；decline 直接完成普通综合。最终同一 Session 可选择当前、已有或新 Question。原 checkpoint、科学内容、DeepFetch 尝试及决定不可变，重启按已有事实继续，不重复 summary、候选或 Question。
