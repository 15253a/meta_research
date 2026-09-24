---
status: accepted
---

# 后继 Cycle 继续实施前重新判断计划

2026-09-17，用户要求 Reasoning 创建下一 Cycle 时，入口只允许 `idea`、`plan` 或 `reasoning`，不能直接进入 `bundle`。此决定收紧 ADR 0003 保留的后继入口与计划复用机制；原有同 Cycle 单向推进、历史不可变和正式 skip 接纳边界继续有效。

需要新构想时进入 Idea。构想仍适用而需要继续实施时，凭可验证的 Idea skip basis 进入 Plan，由 Plan 根据最新证据与上一轮综合明确本轮义务、选择复用证据及剩余工作。已有材料足够且只需综合时，可以在 Idea、Plan、Bundle 的正式 skip 合同均成立后直接进入 Reasoning。Bundle 保留为本轮实施阶段，不再是下一 Cycle 的直接入口。

原因是旧 FormalPlan 同时保存研究承诺与制定时的证据状态；整体复用会连同旧 coverage、gap 和 Brief 一起保留，而这些判断不会随新结果自动更新。新 Plan 应明确哪些已有工作仍适用、哪些实际缺口值得继续投入，避免把新 TargetGraph 尚无结果误判为研究从未开展。本决定不要求每轮重建 Idea、重跑历史工作、取得新发现或按固定轮数推进。

职责保持分离：Reasoning 负责科学综合、下一问题与入口选择，并交接已有认识、证据边界、未决事项和继续理由；Plan 负责本轮承诺与正式 Evidence 选择；Bundle 负责具体 Target、依赖和精确资产输入。文字中的复用意图不能替代正式输入绑定，Reasoning 也不承担逐个文件的调度职责。旧结果、计划、资产版本和接纳证明保持历史身份，不通过改写旧内容制造继承。
