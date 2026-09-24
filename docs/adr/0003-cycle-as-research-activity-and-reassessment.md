---
status: accepted
---

# Cycle 是有研究重心的推进与重新判断

2026-09-13，用户确认 Cycle 类似研究者的一天：根据当前研究状态选择重心与投入，在实施中思考和核验，再综合认识并判断下一步。它不是 Question 与 Target 之间的另一层问题，也不要求解决固定数量的窄未知或每轮产生新发现；Gap 是相对目标和认识的不足，可涉及解释、证据、方法或资源，宽窄随研究而变。

保留现有 Idea、Plan、Bundle、Reasoning 的正式单向交接、后继入口与 skip 机制。已有构想或计划应在适用时正式复用，Bundle 在承诺范围内自主分析、补充核验和滚动实施；核心已承诺科学语义的改变仍通过 Reasoning 与后继 Cycle 处理。该取舍保留已接纳历史的可解释性，同时避免把研究者的每一次思考变成阶段回退或新对象生命周期。Baseline → Variant → VariantRun 与 Evaluation/EvaluationAttempt 层级、Dataset 语义身份及 RM/RG 的既有资产与引用职责保持不变。

Plan 选择本轮承诺和重新判断的检查点，区分局部工作的真实依赖与 Question/Quest 的整体完成标准。默认交接提供当前认识、相关索引和读取入口；完整历史及继续实施所需的实际材料按需取用。Reasoning 交接认识、证据边界与继续、改变或等待的理由；允许无进展，反复无进展则应重审策略。技术重试和补文件沿原有执行恢复处理，不因其发生而自动新增 Cycle；无需增加一套必填研究状态字段。

真实外部依赖继续使用现有 HumanRequest、挂起和原 Session 恢复机制；本次没有增加 Cycle 闭合后的 waiting 状态或第三种 Reasoning transition。等待条件写入研究交接，依赖由现有 Owner 处理，不通过连续空 Cycle 轮询同一障碍。
