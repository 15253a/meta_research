# 0016 · CP3.3 观测摘要进 reasoning 锚点 + status_card 发布（收尾 M2）

- date: 2026-07-08
- commit: 72647f8 — feat: CP3.3 观测摘要进 reasoning 锚点 + status_card 发布（收尾 M2）
- branch: main
- 检查点 / 步: CP3.3（属：步③ M2 上下文编译器 + 召回 + 观测摘要 + status_card）——**M2 收尾检查点**

## 决策
M2 最后一检查点，四件事：

1. **运行观测摘要真渲染**（§4.7，替 CP3.1 占位）：`compiler_sqlite.py` 新增 `_observation_summary`——reasoning
   固定锚从 `execution_observation` 渲机器事实（经 `execution_log(cycle_id=本轮)` 一跳）。确定性纪律：
   ORDER BY eo.id 定序、**只渲机器事实列、不渲 created_at**（DB 插入 wall-clock，两次真实运行会不同→破字节一致；
   `wall_clock_sec` 是 parser 观测值、同快照同值，可渲）。source='codex' 行按 DDL CHECK 机器列恒 NULL →
   只出 digest_ref、不伪造事实。header 显式声明 §3.1.2 铁律（观测只供调试/复现/下一步评估，不得作
   novelty/success/correctness/关问题选择输入）。
2. **status_card 构建器**（新 `status_card.py`）：§4.6.6 封闭字段派生卡（**不在核心 DDL**，可重建；「阶段边界
   原子发布」真接入=M3）。M2 交付：从 DB 真相构建封闭字段集、纯函数可测。M2 无真源字段（selection.latest_decision
   /budget.global_remaining/heartbeat_ref）诚实置 None（字段仍在封闭集，M3 接线填）。
3. **门禁 authorizer 拒读负例**：证同一 execution_observation——编译器普通只读连接读得到、渲进 pack；门禁
   authorizer 只读连接拒读（观测影响 reasoning 但绝不进 gate 判据，§3.1.2 隔离）。
4. **预算去重**：抽 `budgeting.compute_budget` 为 B(t) 唯一定义，compiler `_budget` 委托（防公式两处漂移，§10）。

裁量（全自动）：
- import deferred 不产 target 的 M2 断言**已由既有 `test_isolation_m1c.py`（build_target count=0）覆盖**，本检查点
  不重复造（§7 反过度构建）。
- status_card selection.latest_decision 不写查询、显式 None+TODO(M3)：selection DECISION 审计行由 advance 落（M3），
  且 decision 无 goal_id、须按「选出本轮问题的那次选择」定 scope（非全局最新）——留 None 防 M3 误当已接线。

## 改动文件
- `meta-research/orchestrator/compiler_sqlite.py` — 修改：`_budget` 委托 compute_budget；新增 `_observation_summary`；
  reasoning 锚点占位段换真渲染；模块 docstring 更新（观测段已渲、检索区/引用区接入=M3）。
- `meta-research/orchestrator/status_card.py` — 新增：`build_status_card`（§4.6.6 封闭字段）+ `_pending_file_request`
  （items_json 非数组→item_count None）+ `_first_line` + `status_card_json`（canonical）。
- `meta-research/orchestrator/budgeting.py` — 新增：`compute_budget`（B(t) 唯一定义）。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图加 budgeting / status_card。
- `meta-research/tests/test_observation_summary.py` — 新增：7 测（机器事实渲染/铁律 header/codex digest-only/
  确定性/排除 created_at/无观测诚实/**门禁拒读负例**）。
- `meta-research/tests/test_status_card.py` — 新增：11 测（封闭字段集/各真字段/子字段封闭/预算三元/counts/pending/
  非数组 items/canonical JSON/cycle 不存在）。

## Review
- **内审（Opus 子代理）**：APPROVE。2 SHOULD 修：① 删 `budget.global_spent`（§4.6.6 预算三元 B(t)/本轮已花/
  全局剩余，不多不少；避免应答器"照卡说话"多讲未授权事实）② `selection.latest_decision` 从全局 `LIMIT 1` 查询改
  显式 None+TODO（原查询无 goal scope，M3 会跨 goal 串卡）。另加子字段集断言堵"顶层键测不到子字段扩表"缺口。
- **外审（codex-chatgpt gpt-5.5/xhigh 第1轮）**：APPROVE（无 BLOCKER）。核可：不渲 created_at、eo.id 定序、
  codex 分支只出 digest、header 铁律、门禁负例、封闭字段完整、budget 抽取公式一致。2 SHOULD 修：① items_json 非数组
  （串/对象）→ item_count None（不按字符/键数误报，加回归测试）② goal_body_md 版本绑定契约入 docstring（须调用方按
  cycle.goal_ver 传、勿跨版复用）。
- 未采纳意见：无（两轮 SHOULD 全采纳）。

## 验证
- 命令：`pytest tests/ -q`
- 关键输出：
  ```
  316 passed in 15.96s
  ```
- 观测摘要渲染实样（reasoning 锚点，确定性 + 铁律 header + parser 机器事实 + codex digest-only）：
  ```
  ## 本轮运行观测摘要（§4.7）
  > 用途限定：仅供…；**不得作 novelty / success / correctness / 关问题的选择输入**（I3 铁律，§3.1.2）。
  - [obs1·parser·train] nan=0 发散=0 oom=0 warning=2 retry=1 last_loss=0.12 loss_trend=down wall_clock_sec=123.5
  - [obs2·codex 摘要·train] digest_ref=digest/ref.md
  ```
  status_card 封闭 10 字段：snapshot_cycle/goal/active_question/cycle_status/route/selection/budget/counts/heartbeat_ref/pending_file_request。

### 步级验证（本检查点收尾步③ M2）——跑 §7.1 M2「验证方法」
M2 五条可证伪判据逐条映射到测试（`pytest` 全绿即步级通过）：

- 命令：`pytest tests/test_compiler_sqlite.py::test_render_byte_identical tests/test_observation_summary.py::test_observation_deterministic tests/test_recall_sqlite.py::test_recall_level1_cards tests/test_recall_sqlite.py::test_recall_level2_variants tests/test_recall_sqlite.py::test_recall_level3_reuse tests/test_recall_sqlite.py::test_ctx_fetch_execlog tests/test_recall_sqlite.py::test_reuse_uses_measurement_index_not_full_scan tests/test_observation_summary.py::test_observation_in_pack_but_denied_to_gate tests/test_isolation_m1c.py`
- 关键输出：`23 passed`
- 逐条映射：
  1. **同快照+配方+预算→context_pack 字节一致（diff=0）**：`test_render_byte_identical`（idea/plan/bundle/reasoning 四阶段参数化）+ `test_observation_deterministic`（观测段进锚点后仍字节一致）→ 通过。
  2. **召回四级可停（§3.6.2）**：`test_recall_level1_cards`（卡片 LIKE+faceted tag）/ `level2_variants`（变体矩阵）/ `level3_reuse`（测量索引）/ `ctx_fetch_execlog`（深潜）→ 通过。
  3. **复用判定 O(1)（EXPLAIN 证走测量索引、非全表扫）**：`test_reuse_uses_measurement_index_not_full_scan`（EXPLAIN 断言 `SEARCH e USING INDEX (variant,protocol,ver)` + `SEARCH mr USING INDEX uq_mr_agg` + 无 SCAN e/ea/mr）→ 通过。
  4. **观测摘要进锚点、门禁 authorizer 拒读（负例）**：`test_observation_in_pack_but_denied_to_gate`（编译器读到并渲进 pack；gate 读连接 SELECT execution_observation 抛 not authorized）→ 通过。
  5. **import 仍 deferred、不产 target**：`test_isolation_m1c`（三写入后 build_target count=0，占位 baseline 与问题均无派生 target）→ 通过。
- **结论：步③（M2）步级验证通过。** 全量 316 绿。

## 遗留 / 回退
- 待办（M3 接线）：① 编译器检索区/引用区接 recall_sqlite（按配方召回填 retrieval/refs）② status_card 由 advance
  阶段边界原子发布 + 写 outbox；填 selection.latest_decision（按 cycle scope 查）/ global_remaining（若引入会话级
  预算上限）/ heartbeat_ref ③ 观测摘要的 parser_result_suspect 真派生（M4）后复用判定方可对真执行上线。
- 回退：`git revert 72647f8`（新增 budgeting/status_card + compiler 观测段 + 测试；与 M0 桩并存、未接 driver，回退不破基线绿）。
