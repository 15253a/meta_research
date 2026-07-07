# 0012 · CP2.3 SqliteGate（M1a-Gate：authorizer 隔离 + gate_input 视图 + gate_close_question）

- date: 2026-07-08
- commit: d07c6c6 — feat: CP2.3 SqliteGate（M1a-Gate）
- branch: main
- 检查点 / 步: CP2.3（属：步② M1 资产层落地 · M1a 的 Gate 半）

## 决策
Gate 三级校验换真：新增 `orchestrator/gate_sqlite.py`（SqliteGate），**与 M0 StubGate 并存不替换**
（M0 driver 仍用 StubGate、基线保持绿；端到端切真 loop 归 M3 Advancer）。三件事：
1. **gate_input 隔离（§6.13(2)，机制非约定，三部齐）**：门禁受限只读连接 + SQLite authorizer 拒 9 表
   （含派生视图 v_metric_result_trajectory 经底表 execution_log 自动拒）+ `PRAGMA query_only`（机械只读）
   + gate_input_* TEMP 视图（不进冻结 36/72/29/1 计数）+ 闭包测试。
2. **三级校验**（§4.1.1）：schema / 引用完整性 / 业务门禁。
3. **gate_close_question（§4.1.4）**：I3 校验（读走受限连接）→ 写 answer+evidence+question 迁移+resolve dep
   （写走 WriteDaemon 单事务）；拒记 DECISION(actor=gate,type=reject)。

裁量（全自动模式自主裁决，落本 build_log）：
- **parser_result_suspect = M4**（依赖真 execution_observation；M0–M3 假执行无真观测）。
- **池注册 gate_*（baseline/variant/eval/attempt… 15 个，§4.1.4）= CP2.4**。
- **reasoning-finish 全序原子（gate 关问 + tree_ops + selection 同一事务，§4.2.5(a)）= M3 Advancer**——现
  gate_close_question 自成短事务、StateStore 各自事务，未统一写服务事务归属。
- evidence id 约定 mr<n>/q<n>/d<n>（M1 起 DB 行；供 M2 compiler/M3 对齐）。

## 改动文件
- `orchestrator/gate_sqlite.py` — 新增：SqliteGate（authorizer + gate_input_* 视图 + query_only + 三级校验 + gate_close_question）。
- `orchestrator/__init__.py` — 修改：模块地图加 writedaemon/gate_sqlite/statestore_sqlite。
- `tests/test_gate_sqlite.py` — 新增：authorizer 隔离（9 表+v_trajectory+闭包）+ gate_close_question happy（eval/literature/human/child_answer 子树内）+ 否定（终态/无证据/畸形证据/引用不存在/非成功测量/target_complete/applicability 同版负向/child 无 answer）+ **触发器 ABORT 转干净拒回归** + query_only + preview + dep 解算。

## Review（codex-chatgpt gpt-5.5/xhigh + 内审 Opus 子代理）
- **内审（Opus，带实测探针）**：REQUEST_CHANGES——**1 BLOCKER**：child_answer 过 gate 预检却撞触发器
  （trg_evidence_child_scope 子树 / trg_evidence_child 子问题未关）→ 裸 sqlite3.IntegrityError 逃逸、无 DECISION、无半写回滚缺口（违 §4.1.1）。**3 SHOULD**：门禁读连接非只读（authorizer 只拦读不拦写，可写 decision）；gate_close_question 不校证据形状（缺键→KeyError 非 GateReject）；测试缺 child_answer happy / 子树外拒 / 真 dep / preview。**2 NIT**：target_complete NULL 跳过未注、跨版 child 脆弱、_num None→裸约束错。
  - **全部采纳**：写路径包 try/except sqlite3.Error → _reject（一处封死整类触发器错配）；`PRAGMA query_only=ON`；证据形状再校验（kind+必需键）；cycle_id 顶部校验；补 6 用例（含 BLOCKER pin）；补注释。
- **codex 第 1 轮**：REQUEST_CHANGES——**2 BLOCKER**：① _reject 用无效 FK 写 decision（question/cycle 不存在时撞 FK，「干净拒+记 DECISION」失效）② gate-only 判据（target_complete/applicability，无触发器兜底）校验↔写入 TOCTOU。**2 SHOULD**：except sqlite3.Error 过宽（把 locked/IO 记成 reject）；门禁读连接非 mode=ro（query_only 可翻回可写）。**全部采纳**：_reject 存在性预查→NULL FK+attempted 入 payload（+ 顶部预校验 cycle）；target_complete/applicability 移 `_gate_only_violation` 在**写锁内重跑**（TOCTOU-safe，§4.1.3）；except 收窄为 IntegrityError；读连接改 `mode=ro` URI。+ 4 回归用例。
- **codex 第 2 轮**：APPROVE（无 BLOCKER；SHOULD attempted_question 记原始串 + NIT 去无用 import/URI quote 已采纳）。
- 未采纳意见及理由：无。

## 验证
- 命令：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`
- 关键输出：
  ```
  255 passed
  ```
  （M0 基线 233 + 新增 22 gate）。gate 否定用例经 diag 核验命中预期拒因。
- 步级验证：本检查点未收尾步②（M1c CP2.4 未完）。
- 结论：**通过**。

## 遗留 / 回退
- 待办：CP2.4（池注册 gate_*：baseline/variant/eval/attempt… + M1c 隔离拒绝用例）。
- 悬案（记此）：reasoning-finish 全序原子 = M3；parser_suspect = M4；跨版 child_answer applicability = M3 goal-amend 跑通时验（现由 try/except 兜底为干净拒）。
- 回退：`git revert d07c6c6`（gate_sqlite + tests 新增，__init__ 注释改，无对既有模块行为改动，回退安全）。
