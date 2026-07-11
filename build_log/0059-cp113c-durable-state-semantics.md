# 0059 · CP11.3c 耐久状态语义收口

- date: 2026-07-11
- commit: `a32629da5705b5abf1480db886f147caa9b005c0` — fix: close CP11.3c durable execution state semantics
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.3c（属：步⑪ CP11.3 状态与执行边界）

## 决策

CP11.3b 的 guardian receipt 只证明 OS 进程树事实，不能证明模型信封、训练产物、评估指标、评审或业务 Gate
成功。本检查点把 reference 的 plan/current-lineage/执行 owner 概念接到 SQLite 权威状态面，避免长时间运行后由
隐式内存态、重复外调或崩溃缝隙造成漂移：

- plan target 的 `critical`、有限非负 `budget_estimate` 权威落库；总估算受本轮动态预算约束，critical failure
  触发确定性早退，非 critical failure 允许后继继续。空计划特化为 `reuse_only`，纯既有 checkpoint 评估特化为
  `eval_only`，不伪造训练 run 或改变 pool identity。
- cycle、question、goal/version、target 全链只允许 current lineage；goal amend 前要求旧执行面静默。reasoning 的
  answer/evidence、tree ops、selection 和 cycle 收尾在同一 SQLite 事务内完成，业务拒绝通过 SAVEPOINT 留审计但不
  留半写。
- runner/model 调用先创建 durable `runner_call`，再转 running；同一个 ID 承担 terminal、ledger 与 judge decision。
  train/eval/smoke 分别先创建 `run`、`evaluation_attempt`、smoke `build_target` owner，再允许 guardian spawn，禁止
  “进程已经开始但 DB 不知道是谁”的窗口。
- 新增 startup execution reconciler：只接受 exact owner/context 且每 owner 唯一的 terminal+drained receipt；
  timeout/owner-loss/spawn failure 映射到权威失败或可显式 retry 状态。`exit(0)` 只表示进程结束，绝不合成业务成功；
  owning stage 必须恢复并验证 `.partial`、解析产物、完成评审和 Gate。
- harness 可依据 exact central receipt 恢复 owner 死亡后已完整写出的 partial，并安全重建 exit/process sidecar；
  无 exact receipt、非 regular/private 文件、上下文错配或非 exit outcome 均 fail-closed。guardian heartbeat 增加
  CPU/output/descendant 活动与结构化时序，console 投影只展示有凭据的 live 状态。
- attack/import 的 eval attempt、run 与 smoke owner 均前置；失败通知新增 target/run/attempt/receipt 锚。
  120 轮回归真实推进 120 个 attack-intent cycle 并重启复核状态/投影不漂移；该测试证明控制面长程稳定性，不冒充
  120 次昂贵模型/训练实跑。

## 改动文件

- `meta-research/orchestrator/advancer.py` — 修改：current-cycle guard；接入 `eval_only`/`reuse_only`；goal-amend
  reasoning 前静默检查。
- `meta-research/orchestrator/attack_stages.py` — 修改：plan budget/critical、eval-only 派生和执行、owner-first
  train/eval/smoke、partial recovery、失败/重试与 route specialization。
- `meta-research/orchestrator/compiler_sqlite.py` — 修改：context pack 展示 target critical/budget/failure 与测量锚。
- `meta-research/orchestrator/console_server.py` — 修改：从结构化 guardian heartbeat/receipt 生成诚实 live 投影。
- `meta-research/orchestrator/cost_ledger.py` — 修改：created→running→terminal 的既有 runner_call 收尾、未知用量与
  startup failure 补账接口。
- `meta-research/orchestrator/execution_reconcile.py` — 新增：runner_call/run/evaluation_attempt/build_target 的 exact
  receipt 启动对账，拒绝重复/错配 receipt，禁止由 exit(0) 合成业务成功。
- `meta-research/orchestrator/gate_exec.py` — 修改：target critical 早退、eval attempt 先存后跑、current target
  lineage 与完成凭据约束。
- `meta-research/orchestrator/gate_pool.py` — 修改：预创建 running attempt 的指标/评估原子注册与 canonical attempt
  收口。
- `meta-research/orchestrator/gate_sqlite.py` — 修改：current active-question lineage；支持调用方事务内原子关问与
  SAVEPOINT reject 审计。
- `meta-research/orchestrator/harness.py` — 修改：exact-owner staged recovery、private/no-follow exit sidecar、短读写
  防护与 central receipt 指针。
- `meta-research/orchestrator/import_worker.py` — 修改：import run/eval/smoke owner 前置、显式 retry、partial recovery
  与 import-worker cycle lineage 锚。
- `meta-research/orchestrator/importer.py` — 修改：导入候选/问题绑定的 current goal lineage 校验。
- `meta-research/orchestrator/interfaces.py` — 修改：cycle route 接口加入 `eval_only`/`reuse_only`。
- `meta-research/orchestrator/manifest.py` — 修改：允许且仅允许显式 checkpoint capability 路径，向 supervised harness
  传递 execution context。
- `meta-research/orchestrator/notify.py` — 修改：幂等 build-target failure 事件携 run/attempt/execution receipt 锚。
- `meta-research/orchestrator/process_supervisor.py` — 修改：耐久 guardian heartbeat、活动采样和 receipt context 校验。
- `meta-research/orchestrator/run.py` — 修改：默认启动前执行 receipt→DB reconciliation，并传递 cost ledger。
- `meta-research/orchestrator/runner.py` — 修改：Codex 调用绑定 exact runner_call context，保留 supervisor failure receipt。
- `meta-research/orchestrator/stage_provider.py` — 修改：所有 model/judge call 先建 runner_call；heartbeat、terminal、cost、
  decision 使用同一 ID；eval-only review subject 包含既有 checkpoint 和 attempt log。
- `meta-research/orchestrator/statestore_sqlite.py` — 修改：current goal/cycle/question lease、goal-amend/revalidate lineage，
  原子 reasoning helper 与 terminal-cycle 不变量。
- `meta-research/orchestrator/writedaemon.py` — 修改：暴露 active transaction ownership/savepoint 所需的最小能力。
- `meta-research/schemas/plan.schema.json` — 修改：eval create target 必须声明可解析的 `baseline_ref + variant_key`，
  保持执行命令不进入 plan 层。
- `meta-research/tests/fixtures/valid/plan/eval_create_evaluation.json` — 修改：对齐收紧后的 eval target 身份契约。
- `meta-research/tests/test_attack_advance.py` — 修改/新增：critical/预算、eval-only、pre-spawn owner、partial recovery、
  120 轮状态/投影与重启回归。
- `meta-research/tests/test_connectors.py` — 修改：对齐 runner_call terminal/cost 新语义。
- `meta-research/tests/test_cost_ledger.py` — 修改：覆盖既有 created/running call 的失败补账。
- `meta-research/tests/test_driver.py` — 修改：对齐 plan eval identity fixture。
- `meta-research/tests/test_execution_reconcile.py` — 新增：exit(0) 不伪造成功、owner-loss、timeout、duplicate/context
  mismatch、smoke cascade、skipped-with-receipt 与幂等启动对账。
- `meta-research/tests/test_frozen_contracts.py` — 修改：有意收紧 plan eval identity 后重锚字面 hash，继续禁止执行字段。
- `meta-research/tests/test_gate_exec.py` — 修改/新增：critical 早退、running attempt 与 target-owned success 约束。
- `meta-research/tests/test_gate_pool.py` — 修改/新增：预创建 attempt 原子注册、失败与 canonical 不回退。
- `meta-research/tests/test_gate_sqlite.py` — 修改/新增：active lineage、调用方事务整体回滚与拒绝审计。
- `meta-research/tests/test_goal_amend_control.py` — 新增：stale cycle 不得绑定新 goal-amend route。
- `meta-research/tests/test_import_worker.py` — 修改/新增：import attempt pre-spawn、失败 attempt 审计与 pool 不发布。
- `meta-research/tests/test_m4_semantic_cases.py` — 修改：旧 gate 场景补 current active-question lineage fixture。
- `meta-research/tests/test_m6_mechanism_scenarios.py` — 修改：import eval 失败改验预调用 evaluation/attempt 审计；补 current
  question fixture。
- `meta-research/tests/test_obs_parser.py` — 修改/新增：exact-owner partial 恢复、无 receipt 不猜配与 sidecar 写入回归。
- `meta-research/tests/test_process_supervisor.py` — 修改：running receipt fixture 加结构化 heartbeat/activity 字段。
- `meta-research/tests/test_runtime_control.py` — 修改：post-stage priority 场景补 active-cycle 审计锚。
- `meta-research/tests/test_stage_provider.py` — 新增：eval-only result reviewer 可见 checkpoint 与 attempt log。
- `meta-research/tests/test_statestore_sqlite.py` — 修改/新增：active lease、stale selection、terminal guard、revalidate parent
  与 atomic reasoning 回归。

## Review（codex-chatgpt gpt-5.5/xhigh）

- 内部：当前会话没有可用的 superpowers 子代理能力，且上级指令禁止另起协作 sub-agent；主 agent 逐模块审查
  owner/context、transaction、recovery 与 target route，并用相关故障测试收口。提交前额外发现并修复 exit sidecar
  短读、skipped target 存在 execution receipt 两个边界。
- 第 1 轮：`codexro-review` 独立账号 access/refresh token 已失效，HTTP 401 `token_invalidated`，未生成输出文件或
  模型 verdict。
- 第 2 轮（上限）：用完整 staged diff（382394 bytes）走 `codex-chatgpt` 只读内联降级；模型进入审查后连接
  连续耗尽 WebSocket 5/5 重连，再降级 HTTPS，仍长期 idle、未生成输出文件或任何代码意见，最终结束挂起进程。
- 两轮均为审查基础设施失败，不伪报 APPROVE；已到 CLAUDE.md §2.2 上限，不发第 3 轮。prompt 证据为
  `/tmp/review_prompt_cp113c_r1.txt`、`/tmp/review_prompt_cp113c_r2.txt`。

## 验证

- 开发期按用户要求只跑相关验证，关键批次：
  - exec/pool/sqlite gates：`78 passed`；state/advancer/goal/compiler：`101 passed`。
  - StageProvider/cost/runner usage：`102 passed`；process supervisor/console server：`80 passed`；run assembly：
    `45 passed`。
  - schema/driver：`77 passed`；attack 全模块（含 eval-only 与 120-round）：`59 passed in 48.12s`。
  - import/reconciliation：`20 passed`；最终 exact recovery/reconcile 收口：`13 passed in 5.21s`。
- 提交边界只运行一次全量：`pytest -q meta-research/tests` →
  **`5 failed, 1235 passed in 277.44s`**。五项均为有意契约变更后的旧测试锚：plan schema 字面 hash、两个
  suspect-evidence 场景缺 current active lineage、import eval 失败仍断言“无 evaluation”、priority 场景缺
  `active_cycle`。逐项更新后只运行这五项 exact tests → **`5 passed in 1.59s`**；依用户指示未运行第二次全量，
  未伪报全量全绿。
- `git diff --check`、`git diff --cached --check` 均通过。
- 结论：相关状态机/故障路径与唯一全量暴露的五个旧契约锚均已定向通过；全量只运行一次，其历史结果如实保留。

## 遗留 / 回退

- 默认 `run.py` 尚未装配/驱动 `ImportWorker.materialize_pending()`；plan `import_defer` 仍被拒，reference 的
  `import_register → dependency_wait → materialize → release` 生产链尚未完成。
- plan 阶段目前只有 schema/产物重试，没有 reference 要求的独立 answerability review（最多两轮）。
- eval-only 当前只接受恰一个 checkpoint；checkpoint 先验 hash 后仍按路径交给子进程，存在 hash/open TOCTOU。
  完整 content-addressed artifact capability、provider invocation/billing exactly-once 仍待 CP11.4。
- 现有 guardian 是可信同机 descendant-tree lifecycle，不是 container/cgroup/VM 敌对隔离；120 轮测试证明状态面
  长程稳定，不等价于 120 次真实外调、训练与跨节点运维验收。
- 回退：先停止 orchestrator/connector/guardian/payload，备份 DB、staging 与 `state/executions/`，确认无 running
  receipt，再执行 `git revert a32629da5705b5abf1480db886f147caa9b005c0`。本提交无 DDL migration，但旧代码无法
  安全接管仍活着的新 owner protocol 执行。
