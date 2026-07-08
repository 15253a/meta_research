# 0018 · CP4.2 外层驱动循环 + decompose advance + kill-9 恢复

- date: 2026-07-08
- commit: ff30463 — feat: CP4.2 外层驱动循环 run_cycles + decompose advance + kill-9 恢复（M3）
- branch: main
- 检查点 / 步: CP4.2（属：步④ M3 编排器 Advancer + 恢复 + import 降级）

## 决策
CP4.2 交付 M3 的**恢复**验收（§7.1 M3 首判据）。范围裁量（全自动，ROADMAP 步④）：reasoning-only 轮
（bootstrap→decompose→terminate）；attack 轮 idea/plan/bundle 阶段恢复依赖 pool 注册 gate_register_* + 真执行 →
归 M4（M4 扩恢复测试到 attack 阶段）。M3 交付恢复**机制**（atomic 阶段 + cycle.status 游标 + durable 交接 + 幂等）。

三部分：
1. **run_cycles 外层驱动循环**：全程从 DB 读、**无进程内记忆**——重启新实例指向同库即续跑（durable 交接：下一轮
   route/目标由上一 done 轮的 selection 定，非内存持有）。每轮 `_resume_or_open`（在途轮续跑 / 据上一 done 轮
   selection 开新轮；**开轮前**拒不支持的 intent，免留 route=None 死轮）→ `_setup_cycle`（**单一 atomic**：定 route
   [首轮 bootstrap，其后 derive_next_route→decompose] + decompose 激活目标问题）→ advance。上轮 next_intent=terminate → 停机。
2. **decompose advance**：`_reasoning_cycle` 统一 bootstrap/decompose——长操作（render/provider）事务外（§6.13 铁律），
   短写序列（apply_tree_ops + persist_selection + mark_done）裹**单一 atomic** + 事务内二次核终态/route（TOCTOU）；
   fail-closed（bootstrap 须 create_root、decompose 须 add_children）。
3. **statestore**：`inflight_cycle()`（在途轮，不创建）+ `last_done_cycle()`（durable 交接源）；抽 `_inflight_row()` 共用查询。

**恢复正确性关键**：`(set_route, activate_question)` 落在**同一 atomic**（`_setup_cycle`）→ `route!=None ⟺ 目标已激活`
（无分裂态）。故 kill 落在 setup 提交后、stage 提交前 → 重启 `inflight_cycle()` 返回该轮、`route!=None` 跳过 `_setup_cycle`
（不重复激活已 active 的问题）、advance 对已激活父跑 stage。kill 落在 setup 前（route=None）→ 重做 setup（父仍 open 可激活）。

## 改动文件
- `meta-research/orchestrator/advancer.py` — 修改：加 run_cycles/_resume_or_open/_setup_cycle；advance 扩 decompose；
  _bootstrap_cycle→统一 _reasoning_cycle + _validated_ops（按 route fail-closed）。
- `meta-research/orchestrator/statestore_sqlite.py` — 修改：加 inflight_cycle()/last_done_cycle()/_inflight_row()。
- `meta-research/tests/test_advancer.py` — 修改：加驱动循环全跑 + in-process resume-after-restart + **真 kill-9 subprocess
  恢复** + _final_state 确定性比较助手；test_advance_attack_not_implemented（重命名，attack=M4）。

## Review
- **内审（Opus 子代理）**：APPROVE（无 BLOCKER）。逐一追踪所有恢复路径：setup/stage 分离的 durable gap 由
  `(set_route,activate)` 同一 atomic 兜住（route!=None ⟺ 已激活）；last_done_cycle 目标绑定正确；_final_state 所比列
  在杀 vs 不杀两跑确定一致（rowid 非 AUTOINCREMENT、kill 落在 stage 分配行之前）；kill-9 测试真证「阶段未提交→续跑」
  （marker 在 sleep 前）。2 NIT 全修（_setup_cycle attack 诚实 M4 信号、inflight 查询共用 helper）。
- **外审（codex-chatgpt gpt-5.5/xhigh 第1轮）**：APPROVE（无 BLOCKER）。核可核心恢复路径。2 SHOULD+2 NIT 全采纳：
  ① _final_state 补 next_question_id 等确定性列（durable handoff 核心字段）② 开轮前拒不支持 intent、免留死轮
  ③ prior 读入 _setup_cycle 的 atomic（同快照）④ _reasoning_cycle 二次核 route（TOCTOU 契约完整）。
- 未采纳意见：无（内审 + 外审全采纳）。

## 验证
- 命令：`pytest tests/ -q`
- 关键输出：
  ```
  336 passed in 22.32s
  ```
- kill-9 恢复验收（`tests/test_advancer.py::test_kill9_recovery_final_state_identical`）：subprocess 进 decompose 轮阶段
  将写未写时 SIGKILL → 全新实例续跑 → `_final_state(杀后续跑) == _final_state(不杀跑完)`（排除 timestamp/attempt_id/
  log offset）。稳定通过（内审复跑 5/5，~0.7s）。
- 结论：通过。（CP4.2 未收尾步④；M3 收尾在 CP4.3 import 隔离后跑 §7.1 M3 步级验证。）

## 遗留 / 回退
- 待办：CP4.3 import deferred 隔离全链（三写入 + dependency_wait + 不产 target + pending dep 排除调度 + 不重复登记）——
  收尾 M3、跑 §7.1 M3 步级验证。attack 轮 idea/plan/bundle 阶段 + 池注册 gate_register_* + 真执行 = M4。
- 回退：`git revert ff30463`（advancer 加驱动循环/decompose + statestore 加两读方法 + 测试；与 M0 driver 并存、未接
  M0 主循环，回退不破基线绿）。
