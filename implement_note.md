# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-10 ｜ 位置：步⑪ CP11.2b.3c 真正只读 Codex query responder
- 检查点状态：空闲；CP11.2b.3b 已提交并记账，控制面总检查点仍未完成

## 正在做什么

CP11.2b.3b 功能提交 `2dfa653` 已把 `goal_amend` 闭成真实耐久版本控制：确认的人类 decision.effect 是
精确权威；专用 reasoning-only 轮原子创建不可变 vN+1、迁移并重评未决前沿；closed answer/evidence
不重开而走 applicability/revalidate；route/consume/apply 各崩溃窗可恢复。记录见 build_log 0053，
控制面/调度面 `374 passed`、全量 `992 passed`。

下一检查点是 CP11.2b.3c 真正只读 Codex query responder。其后仍有真实 connector 投递、CP11.3/CP11.4
和字面 100+ 轮生产验收；完成前不得把 CP11.2/CP11.2b 或 M6 数百轮验收标为完成。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- 已提交：CP11.1 `ac53516`；CP11.2a `c077cd2`；CP11.2a.1 `3c4c9b4`；
  CP11.2b.1–2 `10215db`；CP11.2b.3a `7f4fd73`；CP11.2b.3b `2dfa653`
  （对应 build_log 0048–0053）。
- CP11.2b.3b 外审两轮均 `REQUEST_CHANGES`：第 1 轮 2 BLOCKER + 1 SHOULD + 1 NIT；第 2 轮
  1 BLOCKER + 1 SHOULD。两轮上限后全部本地修复，未启动第 3 轮。最终全量
  `992 passed in 195.27s`。
- 当前无未提交功能 WIP；本记账提交完成后工作区应 clean。

## 下一步动作

1. 对照 reference §4.6.2 的中介/只读应答契约，列 query 调用、成本、reply 和失败终态的事务顺序。
2. 把模板 responder 升级为独立只读 Codex 调用：只读快照 + authorizer，不给 DB 写凭据，不接研究 Runner 状态。
3. 为外部调用完成但回执未落库的崩溃窗定义耐久 intent/receipt 或显式失败收敛，并补重启/超时负例。
4. 完成 CP11.2b.3c 的评审与独立提交；再做 b.3d connector、CP11.3/CP11.4 与真实 100+ 轮验收。

## 关键上下文 / 坑

- `reference/` 是设计文档，不是运行时输入；只有进入 prompt/skill/schema/policy/DDL/代码和反例测试的概念才算实现。
- HTTP 进程必须保持 DB `mode=ro`；所有状态效果只允许 run 进程的 WriteDaemon 路径。
- reasoning 控制输入上限只统计 `consume_at=reasoning_start`；绝不能让 note 洪泛拒掉 resume/abort。
- `set_budget` 的耐久权威是与 directive `consumed_decision_id` 对账的完整 decision.effect.budget；禁止新增进程内 override。
- active Qn 是 reasoning 收尾后的合法 selection 候选；reprioritize 消费期只允许“当前 cycle 的 active_question_id”例外。
- `goal_amend` 的权威是已消费 human decision.effect，不是可截断 polished；专用轮只消费 amendment，积压 note/优先级延后到新版下一 reasoning 边界。
- 改版只迁移 open/inconclusive；closed question/answer/evidence 永不重开。活库已生成 v2+ 后不能靠 Git revert 自动删历史，回退须 pause + 兼容部署或恢复 DB 快照。
- query responder 必须真正只读且有可审计调用/成本回执；当前模板 grounding 不可冒充 reference 的 Codex responder。
- connector 投递与供应商调用都存在“外部成功、持久回执前 SIGKILL”窗口；CP11.2b.3 先闭通知投递，通用调用补账仍属 CP11.4。
- CP11.3/CP11.4 和 reference M6 数百轮真实验收未完成，所以当前仍是受约束的 operational canary，不是最终生产级系统。
