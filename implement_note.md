# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-10 ｜ 位置：步⑪ CP11.2b.3b goal_amend 真实语义
- 检查点状态：在建；CP11.2b.3a 已提交，控制面总检查点仍未完成

## 正在做什么

CP11.2b.3a 功能提交 `7f4fd73` 已把动态 `set_budget`、账本/状态卡对账与 `pin/boost/suppress`
闭成真实耐久语义：预算消费者重启后同源投影，优先级在 selection 提交点机械执行，确认前零效果，
坏 selection 与不可用控制均有终态审计。记录见 build_log 0052，全量 `976 passed`；CP10.3 同时收口。

当前进入 CP11.2b.3b：实现 `goal_amend` 的目标新版本写入、reasoning-only route、旧答案
applicability/revalidate 和崩溃恢复。其后仍需真正只读 Codex query responder 与真实 connector 投递。
完成这些子项前不得把 CP11.2/CP11.2b 或 M6 数百轮验收标为完成。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- 已提交：CP11.1 `ac53516`；CP11.2a `c077cd2`；CP11.2a.1 `3c4c9b4`；
  CP11.2b.1–2 `10215db`；CP11.2b.3a `7f4fd73`（对应 build_log 0048–0052）。
- CP11.2b.3a 外审两轮均 `REQUEST_CHANGES`；第 1 轮 3 BLOCKER，第 2 轮 1 BLOCKER + 1 SHOULD + 1 NIT，
  两轮上限后全部本地修复，未启动第 3 轮。最终相关 `234 passed`、全量
  `976 passed in 191.24s`。
- 当前无未提交功能 WIP；本次仅待完成 build_log/ROADMAP/implement_note 记账提交。

## 下一步动作

1. 从 reference 的 goal version / goal_amend / applicability 契约列出事务全序与拒绝判据，复用现有封闭 tree op。
2. 接 `goal_amend` directive 消费到新 goal version 与下一轮 `route='goal_amend'`，保证旧轮仍绑定旧 goal_ver。
3. 补首次执行、阶段间 kill/restart、spawn/revalidate 上限及旧答案 applicability 正反例，走检查点评审与提交。
4. 再做 CP11.2b.3c query、b.3d connector；随后 CP11.3/CP11.4 与真实 100+ 轮验收。

## 关键上下文 / 坑

- `reference/` 是设计文档，不是运行时输入；只有进入 prompt/skill/schema/policy/DDL/代码和反例测试的概念才算实现。
- HTTP 进程必须保持 DB `mode=ro`；所有状态效果只允许 run 进程的 WriteDaemon 路径。
- reasoning 控制输入上限只统计 `consume_at=reasoning_start`；绝不能让 note 洪泛拒掉 resume/abort。
- `set_budget` 的耐久权威是与 directive `consumed_decision_id` 对账的完整 decision.effect.budget；禁止新增进程内 override。
- active Qn 是 reasoning 收尾后的合法 selection 候选；reprioritize 消费期只允许“当前 cycle 的 active_question_id”例外。
- query responder 必须真正只读且有可审计调用/成本回执；当前模板 grounding 不可冒充 reference 的 Codex responder。
- connector 投递与供应商调用都存在“外部成功、持久回执前 SIGKILL”窗口；CP11.2b.3 先闭通知投递，通用调用补账仍属 CP11.4。
- CP11.3/CP11.4 和 reference M6 数百轮真实验收未完成，所以当前仍是受约束的 operational canary，不是最终生产级系统。
