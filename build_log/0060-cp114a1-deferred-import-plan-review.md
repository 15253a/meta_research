# 0060 · CP11.4a.1 冻结 import 消费恢复与独立 plan 评审

- date: 2026-07-11
- commit: `43c4134e4ee93c7ff0c5eeb8ee4bf3c1d0f22416` — fix: close CP11.4a.1 deferred import recovery and plan review
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4a.1（属：步⑪ CP11.4 残余架构边界；CP11.4a 父项尚未完成）

## 决策

本检查点只闭合“已登记冻结候选”的消费、等待、物化恢复和 plan 独立评审，不把尚不存在的
`repo-search → import_register` 生产入口伪装成完成。CP11.4a 因而拆成两个可独立回退的子检查点：a.1 本提交，
a.2 紧接补生产发现/登记与 license 来源，父项继续保持未勾选。

- plan 只看本 action-cycle 的 candidate/license 冻结摘要；候选集与 license 快照哈希只含内容，不含 SQLite
  surrogate id。`import_defer` 必须回引 candidate、license、selection 与 policy 四锚，提交事务内全部重算，
  render 后追加 license event 会拒收。
- `selected_for_materialization + baseline(planned) + question_dep(pending) + dependency_wait route + release Qn +
  mark cycle done + phase_commit` 在同一事务完成；不产 plan target、不进 bundle/reasoning。
- Advancer 在无人等待问题调度时每个 outer round 最多消费一个 import selection；worker outcome、target/phase
  suffix 和依赖释放可崩溃重放。物化终败原子写 `materialize_failed`、`baseline(build_failed)`、exact dep
  `blocked` 与失败 decision，让问题回到重规划集合，不永久 pending。
- 默认 fetcher 只解码、限额并复核内容寻址的冻结文件、argv、required metrics 与供应链闭包；默认 adapter 标为
  `requires_adversarial_sandbox`。当前 lifecycle supervisor 不是安全沙箱，故正式装配 fail-closed 并通知/重规划，
  绝不在 host 裸跑不可信 adapter。可信注入式 provider 仍覆盖完整 smoke、双评审、factory eval 和 publish。
- plan answerability reviewer 与 generator 分会话，只见最终 draft + selected idea；最多两轮。每次调用先建
  runner_call/成本 owner，verdict 与 cost 原子落库；draft、decision、sidecar exact hash 恢复，第二轮仍 fail
  作为正常 plan_rejected/inconclusive 收尾。transport/timeout 等 RunnerError 不冒充 artifact retry。

## 改动文件

- `meta-research/README.md` — 更新 CP11.3c 已完成事实，并明确 import discovery/sandbox 的诚实边界。
- `meta-research/orchestrator/advancer.py` — 装配/驱动 import queue、恢复 worker cycle、dependency_wait 多依赖与 blocked
  逃生；goal amend/terminate 优先于启动新 worker。
- `meta-research/orchestrator/attack_stages.py` — import_defer 单事务收尾；独立 plan review 两轮、持久 draft/verdict/
  sidecar 与恢复；非有限/缺失 plan 正常业务拒。
- `meta-research/orchestrator/compiler_sqlite.py` — plan 冻结 import 摘要、四锚、物化失败回流；独立 reviewer pack 与
  generator 修订反馈；reasoning 可见本轮 plan 失败。
- `meta-research/orchestrator/import_fetcher.py` — 新增默认冻结 snapshot 解码/限额/哈希/供应链校验器，并声明强沙箱需求。
- `meta-research/orchestrator/import_worker.py` — 默认 fetch 接线、bounded plan_ref/manifest、命令占位解析、queue 限额、
  outcome/target/cycle 崩溃恢复、失败 dep blocked 与错误分类。
- `meta-research/orchestrator/importer.py` — 严格 snapshot/license 登记、内容哈希冻结、确定性选择、license TOCTOU 锚和
  plan phase transaction 三写入。
- `meta-research/orchestrator/notify.py` — 新增幂等 external import failure 通知与有界原因投影。
- `meta-research/orchestrator/run.py` — 默认装配 PlanReviewProvider、FrozenCandidateFetcher 和同 owner ImportWorker。
- `meta-research/orchestrator/stage_provider.py` — 新增成本/heartbeat/verdict 耐久的 PlanReviewProvider；JudgeProvider
  支持 import staging 布局。
- `meta-research/prompts/skills/plan/SKILL.md` — plan/import_defer 四锚、dependency_wait 及独立 reviewer 契约。
- `meta-research/schemas/plan.schema.json` — import_defer 加 license snapshot hash，并封闭为无 protocol/metric/target 的
  专用分支。
- `meta-research/schemas/plan_review.schema.json` — reviewer round/issue/text 的有界封闭契约。
- `meta-research/schemas/policy.schema.json` — plan_review 语义轮次机械上限 2。
- `meta-research/tests/fixtures/invalid/plan/import_defer_with_targets.json` — 对齐新增 license 冻结锚。
- `meta-research/tests/test_advancer.py` — dependency_wait 多依赖/blocked/pending、worker queue 与 terminate 优先级回归。
- `meta-research/tests/test_attack_advance.py` — plan reviewer pass/fail/重启/耗尽、非有限/缺失/身份漂移、import 原子提交、
  license TOCTOU 与上下文隔离回归。
- `meta-research/tests/test_connectors.py` — external import failure 通知回归。
- `meta-research/tests/test_cost_ledger.py` — judge 成本场景改用真实 build_target provenance。
- `meta-research/tests/test_frozen_contracts.py` — 有意收紧 import_defer 后更新 plan schema 字面锚，执行字段语义锁不变。
- `meta-research/tests/test_import_worker.py` — 默认 fail-closed、完整可信链、终败释放、spec drift、outcome crash 与基础设施
  异常不伪装候选失败回归。
- `meta-research/tests/test_isolation_m1c.py` — strict snapshot/license、surrogate-id 无关内容哈希与既有隔离回归。
- `meta-research/tests/test_m6_mechanism_scenarios.py` — 场景 2 对齐终败 dep=blocked 后可重规划语义。
- `meta-research/tests/test_run.py` — 默认 fenced import/reviewer 装配与全 attack 成本/decision E2E。
- `meta-research/tests/test_schemas.py` — policy reviewer 上限回归。
- `meta-research/tests/test_stage_provider.py` — reviewer 成本/重试/pack 身份与 import judge 材料回归。

## Review（codex-chatgpt gpt-5.5/xhigh）

- 内部：上级指令禁止另起协作 sub-agent，主 agent 逐模块审查 transaction/restart/cost/hash/fail-closed 边界；外审前
  发现并修复 surrogate id 进入内容哈希、非有限 plan poison、review draft 身份漂移和 fetch 基础设施错误误分类。
- 第 1 轮：`codexro-review` 独立账号 access/refresh token 均返回 401 `token_invalidated`，未生成模型 verdict。
  证据目录 `/tmp/codexrev.GUvB28/`。
- 第 2 轮（上限）：完整 staged diff 内联只读审查，结论 `REQUEST_CHANGES`：3 BLOCKER、2 SHOULD、1 NIT，输出
  `/tmp/review_cp114a1_r2.md`。
  - 采纳：非有限 draft 必须先机械校验再原子写；补 `license_decision_snapshot_hash` render→schema→commit 全链；
    terminate/goal-amend 先于启动新 import；RunnerError 不走 artifact_parse retry。均补 exact regression。
  - BLOCKER「`plan` 未赋值导致 `UnboundLocalError`」未采纳：函数进入 try 前已有 `plan = None`；补“provider 缺
    plan.json”回归证明会正常 `plan_rejected` 收尾。
  - NIT「内容完全相同候选 tie」未改：DDL `ux_extcand_rev/ux_extcand_norev` 已按同 question、trigger snapshot、URI、
    revision 禁止该重复；排序内容键完全相同必先撞唯一索引。
- 已到两轮上限，不发第 3 轮；成立项全部自行修复后继续提交。

## 验证

- 开发期相关验证：
  - `pytest -q tests/test_attack_advance.py tests/test_import_worker.py` → `84 passed in 60.65s`。
  - `pytest -q tests/test_stage_provider.py tests/test_compiler_sqlite.py tests/test_advancer.py` → `92 passed in 13.80s`。
  - schema/skill/隔离/通知/生产装配与全链组合 → `105 passed in 2.35s`。
  - 内审新增 exact 回归 12 项通过；外审反馈修复后的 schema/import/reviewer/advancer 组合 → `93 passed in 6.14s`。
- 提交边界唯一全量：`pytest -q meta-research/tests` → **`7 failed, 1267 passed in 294.51s`**。七项均为有意
  契约收紧后的旧测试锚：三项 cost-ledger judge 使用不存在 target 999、三项 M6 场景仍断言终败永久 pending、
  一项 plan schema 字面 hash。
- 更新七项旧锚后只跑 exact failures → **`7 passed in 2.96s`**；依用户要求未运行第二次全量。
- `python -m py_compile meta-research/orchestrator/*.py`、`git diff --check`、`git diff --cached --check` 均通过。
- 结论：相关功能、故障恢复和唯一全量暴露的旧锚均已定向通过；唯一全量的历史结果如实保留，不伪报全绿。

## 遗留 / 回退

- CP11.4a 父项未完成：没有生产 `repo-search/import_search → import_register` 入口，也没有可回放的 auto/human
  license provenance；当前只消费受信服务事先登记到 exact action-cycle 的候选。
- 默认 untrusted adapter 在强沙箱落地前必然 fail-closed；large repo clone/LFS、fd-safe artifact capability、
  provider invocation/billing exactly-once 与跨节点 100+ 轮真实 soak 仍待后续检查点。
- 回退前停止 orchestrator/connector/guardian，确认无在途 route=NULL worker 与 running execution receipt，备份 DB/
  staging；执行 `git revert 43c4134e4ee93c7ff0c5eeb8ee4bf3c1d0f22416`。本提交无 DDL migration，但旧代码不识别
  新 plan review sidecar/blocked failure protocol，不应在新 worker 仍活跃时热回退。
