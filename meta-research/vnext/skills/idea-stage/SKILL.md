---
name: idea-stage
description: 在冻结的 accepted Question binding 上形成、评审并提交 IdeaOutcome。
---

# Idea Stage

把当前 frozen binding 转化为可供 Plan 消费的候选全集或有证据边界的负向结果。拥有研究综合、review disposition 与最终修订；把内容 custody、领域接纳和 Stage 推进留给相应 Owner。

只在进入对应分支时读取：

- 核对调用、Submission 或 accepted handoff 的字段时，读取[输入／输出契约](references/io-contract.md)。
- 构造具体 Outcome、执行独立评审、处理 feedback／reconciliation 或评估 Exhaustion 时，读取[候选与闭包契约](references/contract.md)的对应章节。

## 1. 锁定输入

1. 接收已经校验的 runtime binding、frozen ContextPack、`AcceptedQuestionBinding`，以及调用者按该 binding 交付的 accepted Question content data。
2. 核对 Question／Quest／content ref、hash、schema ref 与两类 Owner receipt 均为 exact，且与调用信封一致。
3. 用已绑定 content 的语义对齐候选，但把其 schema 与字段合同视为 opaque 上游事实；不逐字段校验、补写或路由 Question 生命周期。
4. binding 缺失、漂移或不可验证时，返回 typed input error 并停止。
5. 只有缺少人类独有的决定、材料、授权或线下动作而无法安全继续时，请求对应 Owner 创建 typed HumanRequest；只暂停其直接依赖，继续其他安全工作。

完成标准：同一 immutable invocation closure 中的所有 exact binding 一致，accepted Question content data 可读取且与其 ref／hash 相符；没有上游事实被本 Skill 改写。

## 2. 形成一个 Outcome

1. 先读取 ContextPack 中与候选有关的已接纳材料、active guidance、历史和未知边界；形成候选前判断是否需要外部 grounding，需要且 capability 可用时，通过绑定的高层读取操作补充。
2. 新外部材料只有取得 Owner accepted ref／receipt 才是 Evidence；否则保留为 `research_observation | unresolved`。分开陈述 Evidence、Agent inference 与 unknown，并记录实际消费输入的 exact provenance。
3. 守住粒度：Question 固定未知、答案形状与范围；方法名可以在 Question 中，但 Idea 必须增加可检验的机制、条件、干预轴或比较结构及推翻方式，不能只复述“尝试某方法”；精确指标、实验协议、Evidence obligation 与冻结实验承诺留给 Plan。
4. 合并语义重复。只有机制、成立条件、干预轴或比较结构会改变 Plan 的研究承诺时，才保留为不同候选。
5. 形成且仅形成一种结果：
   - `IdeaSet`：一个或多个实质不同候选；可带非约束 recommendation。
   - `NoViableCandidate`：说明当前 frozen binding 下为何没有可负责交给 Plan 的候选，并给出探索范围、证据边界与推翻条件。

完成标准：资料选择与不确定性有记录；Outcome shape 完整，候选 key 唯一且实质不同，provenance、证据边界和 Stage 粒度可审计。

## 3. 独立挑战

1. 首次正式提交前，把完整去重草稿交给独立 advisory reviewer。
2. 要求 reviewer 只检查 binding 对齐、实质重复、证据边界、可证伪性与 Plan 可用性。
3. 为每条 finding 给出唯一 `revised | not_adopted` disposition；声称 `revised` 时实际改变最终 Outcome hash。
4. 一并提交最终 revision 与 review record。Reviewer 不批准结果，也不代替 Owner。

完成标准：findings 全覆盖；draft、final 与 dispositions 的 hash 关系成立。

## 4. 提交并恢复

1. 每次正式写入前重新验证原 invocation closure。
2. 先保存 RM content ref／receipt checkpoint，再提交 RG domain outcome；始终分开保存两类 Owner 状态与 receipt。
3. 按原生状态处理：`rejected` 在同一 feedback loop 实质修订；`stale` 重验 exact binding；`needs_input` 等待精确 recovery；`outcome_unknown` 只对账同一 identity；`technical_blocker` 保留 blocker receipt 后恢复。
4. 一个 submission identity 只绑定一个 immutable payload 与 invocation closure。Payload 或 binding 改变时使用带正确 lineage 的新 identity。

完成标准：每次结果都有原生 status 与 receipt；没有聚合的跨 Owner `success`，没有 unknown 副作用被盲重放。

## 5. 谨慎提出 Exhaustion

只有 `IdeaSet` 与 `NoViableCandidate` 都无法形成或获接纳、探索已无实质不同方向、所有 Submission／feedback 已对账且不存在 HumanRequest、technical blocker、outcome unknown、未消费 accepted outcome 或既有 StageCommit 时，才向 AE 提交 `ExhaustionProposal`。

完成标准：每个 closure gate 都由 live receipt／observation 证明；proposal 不冒充 accepted Outcome 或 StageCommit。

## 收口检查

- 只交付 accepted `IdeaSetRef | NoViableCandidateRef` 及真实 Owner refs／receipts，或保留 typed recovery／pending Exhaustion 状态。
- 保持 `execution completed != content accepted != domain accepted != Stage advanced`。
- 保持调用 ledger 中不存在 `ResearchGraph.create_question`；本 Skill 也不签发 Run、不接纳自己的结果、不选择 canonical Idea、不形成 `StageCommit`。

运行 `python vnext/skills/idea-stage/scripts/test_idea_stage_mvp.py`；完成条件是全套 fixture tests 通过。
