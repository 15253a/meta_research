# 0047 · CP10.2 成本账本与即时预算停机

- date: 2026-07-09
- commit: 03d3ffdc8a388fac330a5c687499f3e25c89c68d — feat: CP10.2 激活成本账本与即时预算停机
- branch: fix/architecture-hardening-20260709
- 检查点 / 步: CP10.2（属：步⑩ M6 硬化 成本记账接线）

## 决策

把 CP10.1 捕获的 runner 用量接入权威成本账本，并把 `budget.session_max` 从休眠配置变成真实运行护栏：

- Stage/Judge 的成功、进程失败、超时、坏信封与非法 artifact 重试均落 `runner_call + ledger`；
- Judge 最终 `runner_call + ledger + decision` 同事务，防裁决已生效但成本丢失；
- 每次 ledger 写入同事务立即求和，越线落 durable `global_stop(budget_exhausted)`，提交后中断同轮后续调用；
- token 汇总未知不再冒充真 0。预算开启时，落 failed runner_call + durable
  `cost_accounting_failed` 后干净停；仅 `session_max=null` 允许零成本 best-effort 审计。

价格是本地 notional 折算，非供应商账单。外部调用完成到 post-call 落账之间的 `SIGKILL` 窗口仍诚实记为后续
「调用意图 + 持久回执 + 幂等补账」硬化项，本检查点不伪称 exactly-once billing。

## 改动文件

- `meta-research/orchestrator/cost_ledger.py` — 新增成本配置预验、policy 内容指纹、用量校验、账本写入、重复拒绝、即时预算停机与计账失败持久停机。
- `meta-research/orchestrator/interfaces.py` — `CallUsage.tokens_known`区分真 0 与未知，默认 unknown。
- `meta-research/orchestrator/runner.py` — 成功/失败/超时均携用量；缺汇总标 unknown；输出读取异常也包装携 usage。
- `meta-research/orchestrator/stage_provider.py` — 每次调用恰记一次，非法 artifact 标 `failed/artifact_parse`；Judge 最终三写原子。
- `meta-research/orchestrator/advancer.py` — 精确捕获预算/计账停机，不误提交在途轮。
- `meta-research/orchestrator/run.py` — 在创建 work/DB 前校验 policy 与成本边界，生产装配 Stage/Judge 共用 CostLedger。
- `meta-research/orchestrator/stopcontroller.py` — 更新成本网已激活的真实语义注记。
- `meta-research/policies/policy.yaml` / `schemas/policy.schema.json` — 新增 `price_per_1k_tokens`；强制显式 `session_max`，仅 null 关网。
- `meta-research/README.md` — 更新 `budget_exhausted` / `cost_accounting_failed` 运维语义与账单边界。
- `meta-research/tests/test_cost_ledger.py` — 新增 40+ 成本、失败、原子性、即时停机、未知用量与配置边界回归。
- `meta-research/tests/test_runner_usage.py` / `test_run.py` / `test_schemas.py` / `test_stage_provider.py` — 扩展 runner、全装配、schema 与显式无预算测试。

## Review

- 内部复核第1轮：`REQUEST_CHANGES`。修复失败调用漏账、Judge 三写崩溃缝、`cost_ledger=None` 绕过、
  `session_max` 缺省关网、重复 ledger、非有限配置/用量、轮内超支等问题。
- 外审模式 A 的 `codexro` 凭证失效（401 refresh token reused），未产生结论；按 CLAUDE.md §2.1 降级模式 B（完整 diff 内联、只读）。
- 外审第1轮：`APPROVE`；2 SHOULD + 2 NIT。修复关网零价格误判下溢、Stage/Judge artifact 审计语义、bool 价格与 Judge purpose。
- 第2轮（上限）：`APPROVE`，无 BLOCKER；2 SHOULD + 1 NIT。按两轮上限后自行采纳：
  `tokens_known` 默认 unknown；`record_ledger_only` 缺 runner 也持久 fail-closed；修正 `money_for` 文档。不开第3轮。
- 证据：`/tmp/cp102-review-modeb.md` 与 `/tmp/cp102-review-round2.md`。

## 验证

- 定向（第2轮前）：
  `python -m pytest tests/test_runner_usage.py tests/test_cost_ledger.py tests/test_run.py tests/test_stage_provider.py tests/test_schemas.py tests/test_advancer.py tests/test_stopcontroller.py -q`
  → `208 passed in 36.33s`。
- 第2轮 SHOULD 修正后成本核心集：`python -m pytest tests/test_cost_ledger.py tests/test_runner_usage.py tests/test_run.py -q`
  → `77 passed in 23.24s`。
- 最终全量：`python -m pytest -q` → `754 passed in 157.61s`。
- `python -m compileall -q meta-research/orchestrator meta-research/tests` 和 `git diff --staged --check` → 零输出，通过。
- 步级验证：步⑩未收尾（CP10.3 对账与真触发留待后续）。

## 遗留 / 回退

- CP10.3：`status_card.cycle_spent` 改读当轮 `SUM(ledger.money)`，补真触发步级验收口。
- 生产 exactly-once billing 仍需调用前意图、持久 stderr/用量回执与幂等补账；当前 README 已显式告知 SIGKILL 窗口。
- 回退：`git revert 03d3ffd`。
