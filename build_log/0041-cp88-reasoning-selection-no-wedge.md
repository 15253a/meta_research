# 0041 · CP8.8 reasoning selection 不可调度不楔死（部署首跑发现的真实楔死修复）

- date: 2026-07-09
- commit: 5e7108d — fix: CP8.8 reasoning selection 不可调度不楔死（M7）
- branch: main
- 检查点 / 步: CP8.8（属：步⑧ M7；部署验证发现的真实 bug 修复）

## 决策
用户要求把做完的系统部署到 `fixed_and_test_factory/meta-research` 并真跑验证（「查看能否正常执行」——
**系统无 web 组件**，是 CLI orchestrator，此处理解为看系统能否跑）。真 Codex 首跑（`--max-cycles 6`）
**实录一个测试未覆盖的楔死 bug**：跑 5 轮成功（3 baseline legal + 真测量 0.97–0.99、全双评审 pass）后 c6
楔死——真 Codex 反复 attack 同一问题，visit 达 policy.question_guard.max_inconclusive_per_question(=3)
上限后该题对 attack 不可调度（is_schedulable 返 False，§4.2.1），但 Codex 仍产 selection `next=该题,
intent=attack` → attack_stages._reasoning_stage 的 persist_selection 抛**未捕获 ValueError**（"目标问题不可
调度"）→ 打死 run_cycles。**且 attack 轮 reasoning persist-then-consume（reasoning.json 持久化）→ 重启
确定性重崩 = 永久楔死**——这是 plan/bundle 已有「Codex 产物站不住→不楔死」纪律在 reasoning selection 侧
的缺口。

修法（不代 Codex 推理，只让站不住的产物干净收尾）：
- `InvalidSelectionError(ValueError)`：persist_selection 判定的「Codex 路由产物非法」专用异常，与编排器
  内部/schema/DB 错误区分（后者仍 fail loud）；子类 ValueError 保既有 `pytest.raises(ValueError)` 断言。
- `persist_selection_safe`：只兜 InvalidSelectionError → 记 decision(selection_invalid) + 改持久 terminate
  干净收尾（route 停机 durable，重启不重崩）。
- compiler._open_set 向 Codex 标注不可调度的题（根因侧：信息不全才选不可调度 attack）。
- advancer._reasoning_cycle **不改**（reasoning-only 不 persist、非法 selection 回滚+重调 provider 可复原，
  既定契约 test_advance_*_rollback 锁）——只 attack 轮持久化 reasoning 是确定性重崩、需 durable terminate。

## 改动文件
- `orchestrator/interfaces.py` — 新增 InvalidSelectionError。
- `orchestrator/statestore_sqlite.py` — persist_selection 5 处校验 raise → InvalidSelectionError。
- `orchestrator/attack_stages.py` — persist_selection_safe（模块级）+ _reasoning_stage 换用。
- `orchestrator/compiler_sqlite.py` — _open_set 标注 attack 不可调度题（同 is_schedulable 口径）。
- `orchestrator/advancer.py` — _reasoning_cycle 加分治注释（行为不变）。
- `tests/test_attack_advance.py` — +2（不可调度不楔死[真实重启全新实例]+ open_set 标注）。

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内审（Opus 子代理）：APPROVE。逐项实证：terminate 回退经 atomic 外层事务连接写 decision（随 atomic
  提交/回滚）、route 停机 durable 重启不重崩；catch 范围紧（仅裹 persist_selection 一句，mark_inconclusive/
  apply_tree_ops 仍 fail loud）；attack/advancer 分治站得住（persist-then-consume 确定性重崩 vs 不 persist
  可复原）；compiler 标注与 is_schedulable 同口径且 pack_hash 确定。采纳 NIT：真实重启用全新实例/连接。
- codex 第1轮：APPROVE。SHOULD（专用异常防未来 KeyError 误吞）已采纳=InvalidSelectionError；NIT
  （propose_prune 枚举名）核实与 schema/skill/statestore 一致、非问题。逐条确认①terminate 闭合永久楔死
  ②catch 分界正确③attack/advancer 分治站得住④compiler 同口径。
- 未采纳意见及理由：无（全采纳）。

## 验证
- 命令：`python -m pytest tests/ -q` → **625 passed**（基线 535 无回归；623 + 2 新；test_advancer 两 rollback 测仍绿）。
- **部署系统真跑验证（用户核心诉求）**：修复后重跑 `python -m orchestrator.run --system-root .
  --work-root runs/second_run --max-cycles 8`（fixed_and_test_factory 部署副本，真 Codex）→ exit 0，输出
  `推进 7 轮：[c1..c7]；停因=score_floor`。**7 轮全 done、零 traceback、零"不可调度"错误**（首跑在 c6 楔死，
  此处顺畅越过）；τ 安全网干净自终止；2 baseline legal + 2 success eval + 真测量 0.99+；**decision 表 1 条
  `selection_invalid`**——真 Codex 又产了不可调度 attack selection，此次**优雅收尾未崩**（正是修复生效
  的活证据）；另 3 条 plan_rejected（graceful 业务拒，系统正常）。
- 结论：通过。真实部署 bug 修复并在部署系统上验证——「全自动不楔死」纪律补齐 reasoning selection 侧缺口。

## 遗留 / 回退
- 遗留：CP8.6b（eval/import/route）+ 运维执行/硬化（成本记账、worktree 隔离、§7.4 真跑）——见 README §7 / ROADMAP。
- 回退：`git revert 5e7108d`。
