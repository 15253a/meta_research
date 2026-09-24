---
name: plan-stage
description: 根据当前研究重心、构想与历史证据选择本轮投入及复盘条件，形成可接纳 PlanDocument；用于 Plan 阶段草稿、独立审阅和按反馈修订。
---

# Plan：选择本轮工作

把已接纳 Question、IdeaSet 与已有研究转化为值得投入的工作和重新判断的检查点。Question 可以跨多轮推进，本轮范围由当前研究需要决定。按根系统提示执行六入口、预算、人类输入和语言偏好；委派子智能体时传递范围与输出语言。

字段语义见[Plan 契约](references/contract.md)；发现、精确引用与反馈处理见[Owner 操作](references/owner-operations.md)。

## 1. 读取当前认识

先读上轮综合、notes、已完成工作和相关证据。默认缺上轮摘要时，用 `research_memory.stage_context.read` 的 `source=question_history` 找已接纳结果，再用 `source=scientific_outcome`、`source_ref=outcome_ref` 读正文；缺摘要不代表没有历史。

明确本轮继续、复用及补充什么。复用 Idea 不继承旧 Plan 的 coverage、gap 或 Brief 状态；仍适用的工作保留精确来源，重做需说明新条件、疑点或预期增量。新 Cycle 或空 TargetGraph 本身不要求重跑。

完成条件：本轮投入依据与历史关系清楚，关键采用材料已读到精确原文。

## 2. 选择义务与检查点

选择非空 obligations：`statement` 表达本轮调查责任，`minimum_support` 和 notes 说明投入依据、拟取得观察与复盘条件。可调查广泛缺口或具体假设，履行意味着做过相关工作并如实报告，不能保证假设获支持或问题被解决。完整性只检查本轮承诺。

每项义务通过 `question_trace` 指向实际相关 Question 字段；`idea_relevance` 仅记录真正影响义务的候选、角色和理由。读完整 IdeaSet 后取舍，无需逐项重复所有候选。

局部工作条件与 Quest／Question 总体完成标准分开。已具备数据、方法和授权的局部工作可先取得限定证据；确需整体资格时在相关 Brief 说明。冻结核心科学语义后，由 Bundle／Target 自主判断实现、实际方法归属、补充核验与顺序；需要改变核心承诺时由 Reasoning 与后继 Cycle 承接。

## 3. 核实证据并形成 Brief

先用相关历史或六入口索引发现，再按需分页和读取精确正文。首目录不限制候选全集，同 Quest 后续发现的已接纳来源可正式选用；未取得身份的新发现先作为研究观察。

逐 obligation 判断 `covered | gap`，写支持主张及边界。`covered` 至少有一个实际 evidence use；无测量观察、分析、负结果、人类输入等可按真实内容使用，来源类型不预设科学价值。`gap` 说明相对义务还缺什么，可有部分证据，但须由一个或多个 ExperimentBrief 覆盖。仍要消费的历史来源选入 evidence use，不能只在 notes 写“复用”。

Brief 用 goal、characteristics、boundary constraints、semantic delta 和真实 Idea 来源说明目的、可接受观察与局部限制。Target、实现和依赖由 Bundle 决定。全部 covered 且无 Brief 时为 `no_new_experiment_required`；存在 gap 且均被 Brief 覆盖时为 `experiments_required`。

完成条件：每项义务有唯一覆盖判断，所有新工作对应真实缺口，复用来源与适用边界可核查。

## 4. 独立审阅与提交

委派原生独立子智能体审阅完整草稿和关键原文；根据自由格式反馈及自身判断修订并交接完整最终内容。同根自查不替代独立审阅，没有具体问题也可改稿。Owner 绑定草稿／最终 hash 并核验科研来源，不要求审查表单、审阅者身份证明或批准记录。

输出 `answer_contract`、`coverage`、`experiment_briefs`、`notes` 等核心字段。首目录外实际读过并采用的来源放入最多 32 项 `additional_evidence_bindings`，精确格式见 Owner 操作。adapter 派生 `evidence_reuse_set`、`gap_set`、`idea_trace`、`bundle_disposition`、`source_bindings` 和合同 hash，不重复手填。拒绝时修订具体原因并保留来源；未知效果先对账原身份。

完成条件：真实 RM／RG 接纳及 AR／AE 交接可验证。Plan 不创建 receipt、StageCommit、Bundle Run 或 Target；无新实验只提供由 AE 核验的 skip 依据。
