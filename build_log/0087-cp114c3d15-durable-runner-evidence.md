# 0087 · CP11.4c.3d.1.5 durable runner-call evidence identity

- date: 2026-07-13
- commit: `b208233c867e05a1965f4b98282d4d73b42d2a0e` — `fix(runner): preserve call evidence across restarts`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3d.1.5（属：步⑪ CP11.4c.3d 基本可用 / 目标生产运行）

## 真实缺口

复核上一检查点的失败 rc5 / 恢复 rc6 时发现，两次调用的数据库身份不同，但旧实现的文件身份只依赖
Provider 实例内 `_call_seq` 和 Runner 实例内 `_call_no`。checkpoint 重启会把两个计数器都重置；同一在途阶段
恢复时，新调用可能与旧调用得到相同 tag。Runner 随后会删除同名 out/events、覆写 prompt，heartbeat 的原子写
也会替换同一路径，导致旧失败证据不可回放。

这不是长跑性能问题，而是跨 checkpoint 审计耐久性问题。采用已有数据库 `runner_call.id` 作为唯一身份，
不新增表、daemon、nonce store 或第二套恢复协议。

## 修改

- `CostLedger.mark_call_running` 可把 heartbeat 路径与 created→running 在同一事务绑定；外部进程尚未启动。
- Stage/PlanReview/Judge 先 `begin_call` 取得 rc，再以 `<phase>-rc<ID>.heartbeat.json` 命名 heartbeat。
- bound `CodexRunner` 的 prompt/output/events tag 使用 `rc<ID>`；无 DB 的 M0/诊断调用仍保留实例内计数。
- `CodexQueryResponder` 的 bound 路径同步从固定 `...-1.events.jsonl` 改为真实 `...-rc<ID>.events.jsonl`。
- execution receipt 原有随机 execution ID、provider receipt 原有 rc ID 与 ledger 对账均未改变。

## 验证与审查

- 精确回归：两个全新 StageProvider 实例在同 cycle 都从 `n1` 开始，但两个 rc heartbeat 路径不同且均存在。
- 精确回归：两个全新 CodexRunner 实例在同目录、同 purpose 下分别绑定 rc41/rc42，两个 prompt/output 均保留。
- 精确回归：真实 CodexRunner（fake execution supervisor）经 `answer_for_call` 返回的 rc77 events 路径确实存在。
- `tests/test_cost_ledger.py tests/test_stage_provider.py tests/test_runner_usage.py tests/test_query_responder.py`
  `tests/test_execution_reconcile.py`：**179 passed in 23.36s**；py_compile 与 `git diff --check` 通过。
- 第一轮只读审计发现 query 硬编码路径回归，已同步修复并补上述集成回归；最终只读终审 **APPROVE**，
  未发现 BLOCKER/Major，确认 lifecycle、成本、receipt 与 query 兼容性保持闭合。

依用户要求，本中间检查点未跑仓库全量。clean-node restore 仍只承诺 `sqlite_truth_only`，不复制历史宿主
transcript；本提交不把“原 work-root 内不覆盖”冒充完整 transcript DR。

回退：`git revert b208233c867e05a1965f4b98282d4d73b42d2a0e`。
