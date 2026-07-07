# 0013 · CP2.4 M1c 隔离拒绝用例（DeferredImporter + InteractionIngest）——收尾步② M1

- date: 2026-07-08
- commit: 1182a5a — feat: CP2.4 M1c 隔离拒绝用例（收尾 M1）
- branch: main
- 检查点 / 步: CP2.4（属：步② M1 资产层落地 · M1c）；**本检查点收尾步② M1**

## 决策
M1 收尾：证 v2.3/v2.4 子系统（外部 import + 人机）在假执行期不破 M0–M3 边界。建最小桩 + 隔离断言：
- `orchestrator/importer.py` DeferredImporter（§3.6.3 降级）：register_candidate / review_license /
  **select_deferred（占位 baseline(planned)+external_import(selected_for_materialization)+question_dep(baseline,pending)
  + import_defer decision，四写同一事务）** / reject_by_license。**无 materialize（M4）**。
- `orchestrator/interaction.py` InteractionIngest（§4.6.2/§7.2④ M1）：inbound（durable interaction_message，幂等）/
  ack（模板 interaction_reply，不写 decision）/ create_file_request（interaction_request pending，一 goal 一 pending）。**无 responder（M5）**。
- `orchestrator/ids.py`：前缀校验 id 编解码（c/q/a），importer/interaction 复用（防类型错 id 静默命中别表）。

裁量（全自动，落本 build_log）：import 是研究决策 → import_defer decision 入账正当（§4.3.2；隔离铁律只禁**人机**
query/reply/ACK 写 decision，§4.6.2）；license scope 匹配 = M4 物化时；formal WriteCommand 队列 = M5 并发；
池注册 gate_*（§4.1.4 其余 15 个）= M3（M1 acceptance 不含，见 build_log 0012 裁量）。

## 改动文件
- `orchestrator/importer.py` / `interaction.py` / `ids.py` — 新增（见上）。
- `orchestrator/__init__.py` — 修改：模块地图加 importer/interaction。
- `tests/test_isolation_m1c.py` — 新增：三条隔离（① 无 target/池/evidence/物化 ② 入站/ACK/请求单不写 decision·不改状态 ③ import deferred + pending baseline dep 阻调度）+ license deny + 幂等 + 一 goal 一 pending + 前缀 id 校验 + goal both-or-neither（10 例）。

## Review（codex-chatgpt gpt-5.5/xhigh + 内审 Opus 子代理）
- **内审（Opus，带实测探针）**：APPROVE（无 BLOCKER）——隔离结构性成立、三写入原子且合全 DDL CHECK/触发器、interaction 只写 interaction_* 表。SHOULD：id 前缀未校验（vs statestore._decode）/ select_deferred 第4写(decision)与「三写入」docstring 不符 + §3.6.3 确认 / API 裸 IntegrityError。NIT：reject 丢 license_review_id / 测试硬化 / scope 注记。**全部采纳**（抽 ids.py 前缀校验；docstring 改「三写入+provenance decision」+ 记 import 决策入账正当理由；inbound goal both-or-neither 前置 ValueError；reject 补 license_review_id；测试硬化 + 前缀/both-or-neither 用例）。
- **codex 第 1 轮**：REQUEST_CHANGES——**2 BLOCKER**：① select_deferred 多写 decision(import_defer) 非「三写入」、污染账本（**内审曾判可接受、codex 判须移除；采纳 codex**——三写入更符 §7.1 验收口径、provenance 在 external_import 已足、消隔离疑点）② select_deferred 未校验 candidate 属传入 question（可错挂）。**2 SHOULD**：reject_by_license 未校验同候选 deny；create_file_request 幂等只查 pending。**NIT**：无回滚证明。**全部采纳**（移除 decision + 负断言；candidate↔question 校验；reject 一致性 + license_review_id 必填；幂等锚 (goal,request_hash) 不限 status；补原子回滚用例）。
- **codex 第 2 轮**：APPROVE（无 BLOCKER/SHOULD；NIT：原子回滚用例原失败在第①写、证明力弱→已改用 candidate_set_hash=None 令**第②写**失败、更强地证同事务回滚）。
- 未采纳意见及理由：无。

## 验证
- 命令：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`
- 关键输出：
  ```
  267 passed
  ```
  （M0 基线 255 + 新增 12 M1c）。
- **步级验证（收尾步② M1，§7.1 M1 行）——通过**。M1 acceptance 逐项↔用例映射（全绿）：
  - M1a DDL 建库（36/72/29/1 + migration/checksum 锁）：`test_database.py` 10。
  - M1a 不变量 I1–I6 + append-only（DB 层否定）：`test_invariants.py` 53。
  - M1a v2.3/v2.4 新表否定用例（append-only/provenance/license/恰一 owner/一 pending/codex 不写机器事实…）：`test_v23_tables.py` 28。
  - M1a 门禁 + 三级校验 + gate_close_question（authorizer 拒 9 表+v_trajectory / target_complete / applicability 同版负向…）：`test_gate_sqlite.py` 22。
  - M1b StateStore 落 SQLite + decompose 释放 + **kill-9 无半写**：`test_statestore_sqlite.py` 30 + `test_writedaemon.py` 5。
  - M1c v2.3/v2.4 表约束 + **M1–M3 隔离拒绝用例**：`test_isolation_m1c.py` 12（+ 表约束在 test_v23_tables）。
  - 合计 M1 新增 160 用例 + M0 基线 107（流程契约） = **267 passed**（`pytest tests/ -q`）。
- 结论：**通过；步②（M1）完成**——资产层（DB / WriteDaemon / SQLiteStateStore / SqliteGate / import·interaction 桩）落真并证不变量 + 隔离；真实组件未接 driver（M3 Advancer 接），M0 driver 仍走桩、基线保持绿。

## 遗留 / 回退
- 步②（M1）完成。下一步 **步③（M2）**：上下文编译器 + 召回 + 观测摘要 + status_card（字节一致 / O(1) 复用 / authorizer 拒门禁读观测）。开工前确认 M2 无 OPEN 阻塞（OPEN #1/#2 已裁 M1）。
- M3 待建：池注册 gate_*（bundle 事实写入）+ Advancer + 恢复 + import 物化降级 + reasoning-finish 全序原子。
- 悬案（0011/0012）：resolve_deps dead_end 依赖；跨版 child_answer applicability。
- 回退：`git revert 1182a5a`（importer/interaction/ids + tests 新增，__init__ 注释改，无对既有模块行为改动，回退安全）。
