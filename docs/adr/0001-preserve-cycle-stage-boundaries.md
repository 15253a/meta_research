---
status: accepted
---

# 保留 Question、Cycle 与主要 Stage 边界

2026-09-07，用户明确要求本轮优化保留主要 Stage 间的设计逻辑，将工作限定于 skill、上下文、工具与系统协作细节；允许提出修订，但同一 ResearchCycle 不得从 Bundle 等后续阶段返回 Plan。依据 [Spec #112](https://github.com/15253a/meta_research/issues/112) 与 [Cycle Resolution #58](https://github.com/15253a/meta_research/issues/58#issuecomment-5231669898)，阶段条件化单调推进，需重做上游时沿已有后继 Cycle 机制处理，正常收口经过 Reasoning。

这保留既有研究承诺与历史可解释性，同时允许当前 Stage 内自主取得材料、修复技术问题和调整未冻结的实现。单主智能体替换四阶段、同 Cycle 回退和以技术阻塞伪造研究收口均不属于本轮优化范围；发现现有代码与此不符时作为实现偏差处理，不据此重定义设计。
