# 0011 · CP2.2 单写 WriteDaemon + SQLiteStateStore（M1b：状态机落 SQLite + decompose 原子性）

- date: 2026-07-07
- commit: be84a90 — feat: CP2.2 单写 WriteDaemon + SQLiteStateStore（M1b）
- branch: main
- 检查点 / 步: CP2.2（属：步② M1 资产层落地 · M1b）

## 决策
把 StateStore 状态机落到 SQLite，经**单写 WriteDaemon**（§6.13(1)/§6.6：全库写一条路、单写连接、短事务、
长操作绝不持事务）；核心验收 = decompose 单事务原子性（§4.2.5：add_children=写子问题+父 active→open 释放+
逐子 question_dep(pending) 同一事务；kill-9 后无「子问题已写但父未释放」半写）。

裁量（全自动模式自主裁决，落此 build_log + implement_note/ROADMAP）：
- **CP 编号顺序调整**：CP2.2=StateStore/WriteDaemon（M1b），CP2.3=Gate（M1a-Gate）。StateStore 更自足、
  供 Gate 共用写服务（§6.6），故先落（ROADMAP 已 swap）。
- **新增真实组件、不替换 M0**：M0 driver 仍用 InMemoryStateStore（基线保持绿）；SQLiteStateStore 是并行真实现，
  端到端切真 loop 归 M3 Advancer（§6.10）。逐里程碑换真、Advancer 收口。
- **排除 close_question**（抛 NotImplementedError 指向 gate_close_question）：关问写 answer+evidence+I3 是业务门禁
  （CP2.3），且证据须引用池注册的真 evaluation/metric_result（CP2.4 才有）——不在 M1b 纯状态机范围。
- 语义逐方法等价 M0 InMemoryStateStore（差别只在真相落 DB、写走短事务）。

## 改动文件
- `orchestrator/writedaemon.py` — 新增：单写连接 + `transaction()` 短事务（BEGIN IMMEDIATE…COMMIT/ROLLBACK，不可嵌套）+ query 只读。isolation_level=None 显式掌控事务。
- `orchestrator/statestore_sqlite.py` — 新增：SQLiteStateStore（cycle 生命周期 / 七 op 树 / dep / 调度可见性 / route / selection / applicability）；`atomic()` 供 M3 裹多方法全序；进程内投影随事务回滚复原。
- `tests/test_writedaemon.py` — 新增：提交/回滚/不可嵌套/回滚后可复用。
- `tests/test_statestore_sqlite.py` — 新增：状态机全覆盖 + decompose 原子性回滚 + **kill-9 子进程无半写** + local_map 回滚无陈旧错绑 + spawn goal_amend 上限只数 goal_amend 路由。

## Review（codex-chatgpt gpt-5.5/xhigh + 内审 Opus 子代理）
- **内审（Opus）**：REQUEST_CHANGES——**1 BLOCKER**：进程内投影 _local_maps 不随事务回滚 → SQLite 复用回滚 rowid 致陈旧 local_key 静默错绑（DB 回滚而投影不回滚 = 本检查点要焊的那层留半写）。**2 SHOULD**：spawn goal_amend 上限误数「本轮全部 spawn」（应只数 goal_amend 路由）；CP 编号与 ROADMAP 漂移。NIT：est_cost 显式 null 语义 / _rconn 注释 / _num 裸 ValueError / 投影无界增长。**全部采纳修复**（投影 snapshot/restore 绑事务边界 + 回归用例；spawn 改 in-memory 计数等价 InMemory；ROADMAP swap；_num_opt；est_cost 缺省保留；mark_cycle_done 清投影）。
- **codex 第 1 轮**：REQUEST_CHANGES——**3 BLOCKER**：① ID 解码不校前缀（activate_question("c1") 静默命中 q1）② add_children 的 max_open_questions 漏算释放的父（+1）③ cycle.active_question_id 不落库（不可恢复）。**4 SHOULD**：WriteDaemon.transaction BEGIN 失败留 _in_txn / COMMIT 失败不回滚；atomic() 吞异常提交半写；max_children_per_node/max_closed_revalidate_per_cycle 可累计绕过；list_schedulable 无 ORDER BY。**全部采纳**（类型前缀解码器 _cnum/_qnum/_anum；open 数 +1 父；active_question_id 落/清；transaction 鲁棒化；atomic 文档契约；两上限改累计；ORDER BY id）+ 5 条回归用例。
- **codex 第 2 轮**：REQUEST_CHANGES（2 轮上限）——**1 BLOCKER**：revalidate 累计上限仍可绕过（ON CONFLICT 未更新 audit_cycle，re-seed 旧轮同版行不计入本轮）。**2 SHOULD**：activate_question 的当前 cycle 子查询不够硬（0 行时激活了却无处落）；resolve_deps 未把 dead_end 当终态解依赖。**1 NIT**：mark_cycle_done 在 atomic() 内不清投影（长跑泄漏）。
  - **采纳**：BLOCKER → ON CONFLICT 更新 audit_cycle（seed+mark）+ 去重 answer_id + 跨轮回归用例；SHOULD-activate → 显式核验恰一非终态 cycle 并用其 id；NIT → mark_cycle_done 无条件清投影 + _bundle_cursor 纳入投影快照（随事务回滚一致）。
- 未采纳意见及理由：**SHOULD「resolve_deps 把 dead_end 当满足依赖」——不改**。理由：① 现状与 M0 InMemoryStateStore 完全一致（本检查点硬约束 = 语义等价 M0，真相侧不擅自改判）；② 规格未明「被 prune 的子问题是否应满足父依赖」，两种解读（父随之停 / 父带 dead_end 聚合）都自洽；③ 该边界（父依赖的子被剪）在 M1b 纯状态机不触发真实流程，留待 M3 Advancer / M6 长跑跑通 decompose→prune→聚合全链时按规格定夺。已在 resolve_deps 码注明为悬案。

## 验证
- 命令：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`
- 关键输出：
  ```
  233 passed
  ```
  （M0 基线 198 + 新增 35：5 writedaemon + 30 statestore_sqlite）。
- decompose 原子性：test_decompose_atomic_rollback_on_guard_violation（异常整批回滚，父仍 active/无子/无 dep）；
  kill-9：test_kill9_mid_decompose_no_half_write（子进程 atomic 内写子问题+释放父后 SIGKILL → 重开库无子、父仍 active）。
- 步级验证：本检查点未收尾步②（M1a 的 Gate 半 CP2.3、M1c CP2.4 未完）。
- 结论：**通过**。

## 遗留 / 回退
- 待办：CP2.3（Gate 三级校验换真 + gate_input authorizer 隔离 + gate_close_question + 池注册 gate_*）；CP2.4（M1c 隔离）。
- 架构：SQLiteStateStore 未接 driver（M3 Advancer 接）；投影耐久（跨重启）是 M3 恢复主题。
- 回退：`git revert be84a90`（writedaemon/statestore_sqlite + tests 均新增，无对既有模块行为改动，回退安全）。
