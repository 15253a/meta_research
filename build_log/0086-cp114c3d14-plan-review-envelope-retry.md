# 0086 · CP11.4c.3d.1.4 plan-review malformed-envelope recovery

- date: 2026-07-13
- commit: `43c0b3840c47c323e4580c3c046584f7a0211881` — `fix(review): retry malformed plan review envelopes`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3d.1.4（属：步⑪ CP11.4c.3d 基本可用 / 目标生产运行）

## 真实故障与决策

在 d.1.3 提交后的全新三轮 smoke 中，c1 bootstrap、c2 decompose、c3 idea/plan 均成功，独立 plan reviewer
随后返回一个 fenced JSON block，但 JSON 对象后又带额外内容，`json.loads` 报 `Extra data`。这是模型调用已成功、
用量与 transcript 都已取得、只需按既有 `flow.retry.artifact_parse` 修正的产物格式问题。

旧 `CodexRunner._parse_envelope` 对“无 fenced block / JSON decode / 顶层或 files 形状非法”都抛默认
`runner_error`；`PlanReviewProvider` 为避免 transport 重发而对所有 RunnerError 直接上抛，结果把可修格式错误
误当基础设施故障，整个入口退出。

采用局部分类与分流，不加新重试器：

- Runner 三类 envelope parse/shape 失败统一 `failure_kind=artifact_parse`，保留既有 usage、transcript、
  execution/provider receipt 回填。
- PlanReview 每次先完成 failed runner_call、成本与 heartbeat，再仅对 `artifact_parse` 进入既有有界重试；
  transport/timeout/runtime/receipt/lifecycle 仍原样 fail-loud。
- 重试用尽只抛一个不重复记账的汇总异常，并保持 `failure_kind=artifact_parse`。

没有改 schema、Gate、review 语义轮数、数据库或科研状态机。

## 改动与验证

- `meta-research/orchestrator/runner.py` — envelope parse/shape 精确错误分类。
- `meta-research/orchestrator/stage_provider.py` — PlanReview RunnerError 分流与 exhausted 分类。
- `meta-research/tests/test_runner_usage.py` — 覆盖无 block、真实 `Extra data`、非 object、files 非 object，
  并锁 usage 保留和 timeout 反向边界。
- `meta-research/tests/test_stage_provider.py` — artifact_parse→反馈重试成功；transport 仍只调用一次。
- 精确 TDD：首次 `2 failed, 1 passed`，修后 `3 passed`；最终精确组合 `7 passed in 4.35s`。
- `tests/test_runner_usage.py tests/test_stage_provider.py`：81 项退出 0；随后同一 `&&` 链的 py_compile 与
  `git diff --check` 执行成功。
- 内部只读终审 APPROVE：确认先 finish failed call、再分流，不 double-finish、不遗留 running heartbeat，
  每次 retry 使用新 runner_call/binding；改动局部，无新增 blocker/major。

## 新鲜真实闭环

同一新鲜 work-root 从失败 c3 的持久 plan checkpoint 恢复，不重跑 bootstrap/idea：旧 rc5 保留 failed，
新 plan-review rc6 success 且唯一 PASS decision；随后 bundle rc7 success、Docker build/run success、code review
rc8 与 result review rc9 success、reasoning rc10 success。目标 complete，run success，产生：

- `mr1`: aggregate accuracy `0.9615`；
- `mr2`: aggregate `Per-seed Accuracy Standard Deviation` `0.001767766953`。

reasoning 关闭 q2 为 answered，evidence 精确落 metric_result 行 1/2；`storage_ops verify` 对 c1-c3 全部
deep verify 通过。随后在同一 snapshot 投递 console query，正式 `--max-cycles 0 --once --no-outbound` 不推进
研究轮，interaction_query rc11 success、`responder_kind=codex`、reply 绑定 snapshot c3。

这证明当前单节点 development 的全新基本闭环可用；不证明 production connector 交付、GPU、双节点、≥200 轮、
fault/restore 组合或 T1/T2。依用户要求，本中间检查点未跑仓库全量，也未另启外部长审。

回退：`git revert 43c0b3840c47c323e4580c3c046584f7a0211881`。
