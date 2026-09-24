# Reasoning 语义契约

## 调用闭包

`ReasoningSkillRequest` 绑定当前 StageRunRequest、Run、Attempt、Fence、根 Session、Cycle、Question、Quest、Goal revision、epoch、ContextPack ref／hash、冻结证据和运行绑定。ContextPack 包含已接纳 Question、文献版本或 none、Idea→Plan→Bundle StageCommit、Plan 证据或 none、TargetCommit 及研究上下文。

Plan 复用保持精确 FormalPlan 和原样 `evidence_reuse_set`，各 `EvidenceReuseLeaf/v1` 绑定 catalog entry／use hashes 与真实来源：测量叶保留实际 MetricResult、EvaluationAttempt、Run／Commit 及 RM／RG receipts；无评价 WorkProduct 保留真实 Run／Commit，不补 EvaluationAttempt；HumanInput、ScientificOutcome、AssetVersion、LiteratureSnapshot 使用各自核实的 `source_binding`，不强加 Target 测量身份。

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

`support_scope`、`limitations`、`causal_interpretation` 和四尺度 `research_synthesis` 表达认识和继续／改变／等待理由，允许认识未变。AE 冻结 graph revision、活动问题、父链、当前 Question 历史页、Goal revision、上游 Commit 和 Target 来源；有界历史页含总量与继续入口，不代表完整历史。正文按当前冻结版本核验，恢复不切换 latest。

## NextCycleProposal

`entry_stage=idea | plan | reasoning`。`typed_skip_basis_refs_by_stage` 恰覆盖入口前的阶段，每项为非空去重依据：idea 无 skip；plan 用已接纳 IdeaSet 覆盖 Idea；reasoning 的 Idea、Plan、Bundle 三项均为 `[source ScientificOutcome outcome_ref]`，由 AE 形成 absent-input skip Commit，不复用旧 Plan／Bundle。Bundle 不是合法后继入口。

RG 在 transition 接纳时重验 QuestionAnchor、present／open 和各依据。ScientificOutcome 只支持上述 absent-input skip，不替代目标 Question 的 IdeaSet 或 FormalPlan。自主创建取得新信息后可重新决定目标、入口及 skip，仍按实际已接纳资产验证。

## 独立审阅与技术边界

审阅按[主 Skill](../SKILL.md)执行。权限以当前 catalog 及配对 reconcile 为准，primary／review 名称不额外收窄已授 Dataset 效果。缺 scoped authority／endpoint／credential、必要操作或相应绑定时，在相应执行前保留技术阻塞；内容 hash、source binding、封闭 schema 或 native Session 不一致时不形成有效交接。未知 provider 结果对账原 operation，不重放。

技术问题不改写为 `insufficient_evidence`；该 disposition 只描述来源可核验时科学支持仍不足。

## AutonomousCreation 与恢复

初始 AR checkpoint 保存执行草稿和 hash；DeepFetch summary 接纳后，同一 native Session 读回并决定 create／decline，failed／cancelled 无 summary 时还可 retry。create 修订后才接纳唯一 ScientificOutcome，再经 HC／RM／AE／RG 创建 Question 并挂文献；decline 直接完成普通综合。最终同一 Session 可选择当前、已有或新 Question。原 checkpoint、科学内容、DeepFetch 尝试及决定不可变，重启按已有事实继续，不重复 summary、候选或 Question。
