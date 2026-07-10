# 0052 · CP11.2b.3a 耐久动态预算与机械优先级

- date: 2026-07-10
- commit: 7f4fd734d49423ee11c5670757d901c328ef66da — feat: 闭合耐久预算与优先级控制
- branch: fix/architecture-hardening-20260709
- 检查点 / 步: CP11.2b.3a（属：步⑪生产硬化 · CP11.2 人类控制闭环；同时收口 CP10.3）

## 决策

把原 CP11.2b.3 拆成可独立回退的小检查点。本次只闭合三件已经有确定机械语义的能力：动态
`set_budget`、账本/状态卡预算对账，以及 `pin/boost/suppress` 优先级控制。`goal_amend`、只读 Codex
query 和真实 connector 投递仍分别留作后续检查点，避免用一个大提交掩盖未完成能力。

动态预算不新增可变 policy 表，而以“最新一条成功消费且与 directive 的 `consumed_decision_id` 对账”的
`directive_set_budget` 人类决策作为耐久权威。该决策保存完整有效预算；编译器、StopController、CostLedger
与 status card 每次都从同一投影读取，重启不会静默回退 YAML。实时只允许修改调度/上限字段，token 价格与
成本记账开关保持版本策略边界，避免旧新账本不可比。

优先级控制不只进入 prompt：StateStore 在 selection 提交时机械归一 `directive_adjust`、重排候选或强制 pin，
并把实际应用/不从/失败写成 decision。硬 pin 只有确认后才覆盖更旧 pin；当前 attack 的 active Qn 可在
reasoning 收尾释放后被重选；不可调度 route、缺字段或坏 selection 均收敛到显式拒绝/`selection_invalid`，
不留下 consumed 但永远无效果的控制指令。

## 主要改动

- `orchestrator/runtime_control.py`、`budgeting.py`、`stopcontroller.py`、`cost_ledger.py`：完整预算投影、动态
  B(t)、实时停机上限、有效 policy 指纹与单次越线停机。
- `orchestrator/console.py`、`notify.py`：确定性预算/优先级语法、确认时序、消费校验与真实效果通知。
- `orchestrator/statestore_sqlite.py`、`statestore.py`、`attack_stages.py`：机械 rerank/pin、route guard、
  score schema 对齐和坏 selection 无楔死收敛。
- `orchestrator/compiler_sqlite.py`、`status_card.py`、`advancer.py`、`run.py`：动态预算 provenance、账本对账，
  以及边界降预算后禁止额外 Runner 调用。
- `tests/test_runtime_control.py` 及相关 console/notify/status 测试：覆盖重启投影、同事务停机、账本指纹、
  pin/boost/suppress、active Qn、确认/拒绝时序和错误收敛。

## Review

- 第 1 轮：`REQUEST_CHANGES`。三个 BLOCKER：未确认的新 pin 会覆盖旧 pin；缺失 score 泄漏裸 `KeyError`；
  attack 到限但 decompose 合法的目标会被错误路由。另有 Console 无 policy 装配陷阱与裸预算字段文档漂移。
  全部修复并加入回归。
- 第 2 轮（最后一轮）：`REQUEST_CHANGES`。一个 BLOCKER：reasoning_start 会提前拒绝当前 active Qn，和
  compiler“收尾后可重选”契约冲突；另报 score 契约注释/InMemory 漂移及 JSON `3.0` 整数语法不一致。
- 按 `CLAUDE.md` 两轮上限不启动第 3 轮；三项均本地修复，随后运行相关测试与全量测试。
- 外审证据：`/tmp/codexrev.YLlHxw/verdict.md`、`/tmp/codexrev.MJYwW3/verdict.md`。

## 验证

- 第一轮修复后的相关控制面回归：`233 passed in 47.56s`。
- 第二轮反馈修复后的相关状态机/控制面回归：`234 passed in 37.45s`。
- 最终全量：`PYTHONPATH=meta-research pytest -q meta-research/tests` →
  `976 passed in 191.24s (0:03:11)`。
- `git diff --cached --check` 与 `compileall` 通过；功能提交未混入 build log/ROADMAP/implement note。
- 结论：**通过**。CP10.3 预算对账已收口；CP11.2b.3 总项尚未完成。

## 遗留 / 回退

- 下一检查点：`goal_amend` 的目标版本写入、reasoning-only route、旧答案 applicability/revalidate 与恢复语义。
- 后续仍有只读 Codex query、真实 connector 投递、CP11.3/CP11.4，以及真实 100+ 轮生产验收。
- 代码回退：`git revert 7f4fd73`。本提交无 migration；既有 set_budget/reprioritize 决策只会成为无读取者的
  append-only 审计事实，不需破坏性数据回滚。
