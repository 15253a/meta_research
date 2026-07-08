# 0017 · CP4.1 Advancer 骨架——derive_next_route + advance bootstrap 创世轮

- date: 2026-07-08
- commit: 148c907 — feat: CP4.1 Advancer 骨架——derive_next_route 全矩阵 + advance bootstrap 创世轮（M3）
- branch: main
- 检查点 / 步: CP4.1（属：步④ M3 编排器 Advancer + 恢复 + import 降级）

## 决策
M3 首检查点。M3/M4 边界裁量（全自动，落 ROADMAP 步④）：M3 交付真 Advancer（真组件上可恢复状态机步进 + 路由派生），
与 M0 driver（走桩 + 真 Codex，M0 验收栈）**并存不替换**；pool 注册类 gate_register_*（M1 未做）与真执行归 M4
（attack 全环入池依赖它们），故 M3 聚焦其 §7.1 验收所需（恢复 + import 隔离，均不需入池）。

CP4.1 交付两半：
1. **`derive_next_route(prev_selection, outcome)`**（§6.13(3) 路由矩阵，纯函数）：terminate→None（停机不改写）；
   decompose→decompose；attack×{含 build/exec→attack；仅 eval→eval_only；空 targets→reuse_only；
   blocked|import_deferred→dependency_wait（优先于 build/exec）}。**fail closed**：无法分类的 attack outcome 报错，
   不静默当全复用。与 M0 driver `_specialize_route` 同口径（M0 保留内联版供 M0 验收栈）。
2. **`SqliteAdvancer.advance(cycle_id)`**：驱动 bootstrap 创世轮——长操作（render/provider=Codex）在事务**外**
   （§6.13 铁律），只把短写序列（apply_tree_ops(create_root) + persist_selection + mark_cycle_done）裹进**单一
   `state.atomic()` 事务**。恢复语义（§4.4.5）：kill-9 前回滚（含进程内投影 `_local_maps` 复原，防 rowid 复用错绑）、
   后跳过；`cycle.status` = 续跑游标；已终态幂等 done + **写事务内二次核终态**（TOCTOU 安全，防并发/重入重复推进）。
   fail closed：bootstrap 必含 create_root 的 tree_ops + 必产 selection。decompose/attack route + 外层驱动循环 +
   池注册 = 后续检查点/M4（advance 对非 bootstrap route 诚实 NotImplementedError）。

## 改动文件
- `meta-research/orchestrator/advancer.py` — 新增：`derive_next_route`（矩阵纯函数）+ `SqliteAdvancer`
  （advance / _bootstrap_cycle）。
- `meta-research/orchestrator/statestore_sqlite.py` — 修改：加 `cycle(cycle_id)` 公有读（Advancer 读续跑游标；不存在→ValueError）。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图加 advancer 行。
- `meta-research/tests/test_advancer.py` — 新增：17 测（derive_next_route 全矩阵 9 + advance bootstrap/幂等/failed 短路/
  atomic 回滚/rebind/bootstrap-必含-create_root/非 bootstrap NotImplemented）。

## Review
- **内审（Opus 子代理）**：APPROVE（无 BLOCKER）。逐一核实：路由矩阵优先级、单一 atomic 事务 + 异常传播、
  create_root 回滚真证、终态幂等短路、local_key 跨 atomic 解析 + 投影复原（依赖 statestore 专测）。1 SHOULD+3 NIT 全修
  （恢复比较排除 timestamp 注记、删死 Literal import、幂等测计数 provider + failed 短路、缺 selection 显式报错）。
- **外审（codex-chatgpt gpt-5.5/xhigh）**：
  - 第1轮 REQUEST_CHANGES（无 BLOCKER，4 SHOULD 全采纳）：① derive_next_route fail-closed（empty_targets 显式 +
    无法分类报错）② atomic 内二次核终态（防并发重复推进）③ bootstrap 必含 create_root（fail closed）④ advancer 层
    rebind 测试。
  - 第2轮 REQUEST_CHANGES（**2 轮上限**，无 BLOCKER；1 SHOULD+1 NIT 均采纳）：SHOULD 产物校验移入 atomic、置于终态
    re-read **之后**（使「已并发推进到终态」优先于「本次产物畸形」误报）；NIT 修 rebind 测试注释（诚实标注 advancer 层
    rollback/retry 覆盖，投影错绑 surgical 证明归 statestore 专测）。
  - 未采纳意见：无（两轮全采纳）。

## 验证
- 命令：`pytest tests/ -q`
- 关键输出：
  ```
  333 passed in 19.41s
  ```
- 结论：通过。（CP4.1 未收尾步④；M3 步级验证在收尾检查点跑。）

## 遗留 / 回退
- 待办（后续 M3 检查点）：CP4.2 外层驱动循环（开轮/派 route/激活目标问题）+ decompose/attack route + **kill-9 恢复测试**
  （任意阶段杀进程重启续跑终库一致，排除 timestamp）；CP4.3 import deferred 隔离（三写入 + dependency_wait + 不产
  target + pending dep 排除调度 + 不重复登记）。pool 注册 gate_register_* + 真执行 = M4。
- 回退：`git revert 148c907`（新增 advancer.py + statestore 一方法 + 测试；与 M0 driver 并存、未接主循环，回退不破基线绿）。
