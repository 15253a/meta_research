# 0010 · CP2.1 冻结 Appendix-A schema 落地 + DB 层不变量否定用例（M1a-DB 半）

- date: 2026-07-07
- commit: 6d45b53 — feat: CP2.1 冻结 Appendix-A schema 落地 + DB 层不变量否定用例（M1a-DB）
- branch: main
- 检查点 / 步: CP2.1（属：步② M1 资产层落地 · M1a 的 DB 层半）

## 决策
把《第一部分》附录 A 的唯一规范 SQLite schema（36 表/72 触发器/29 索引/1 视图）落成字节冻结 migration，
并以代码守卫 + 否定用例证明它在 DB 层焊死 I1–I6 + append-only + v2.3/v2.4 表约束。

- **采纳上一 session 未提交的起步产物**（DDL + database.py + db/README 三裁定），先核验正确性：
  DDL body 与 reference 行 918–1614 **字节一致**（diff=0，且加 test_migration_matches_reference_appendix_a 守住）；
  建库计数 36/72/29/1；checksum 锁生效。
- **三项 OPEN 裁定**（用户 2026-07-07 授权全自动模式自主裁决，不再停下问用户）落 `db/README.md`（受审载体）：
  ①附录C不扩键（M4 补）②假 evaluation 入库 source=factory + 三重披露（**写路径约定、非 DB 不变量**）、
  产物 schema 'fake' 枚举 M1 移除 ③三项可选 DDL 全不焊入、走代码层（CP2.2/CP2.3）。
- 影响面：新增资产层建库入口 `database.connect`，后续 CP2.2 Gate / CP2.3 StateStore 均建其上；不改任何 M0 既有行为。

## 改动文件
- `db/migrations/0001_appendix_a.sql` — 新增：附录 A 字节冻结 DDL（逐字摘录）。
- `db/README.md` — 新增：治理说明 + 三 OPEN 裁定（受审载体；裁定②补写路径约定澄清）。
- `orchestrator/database.py` — 新增：建库/开库守卫（**每次 connect** 校 migration checksum + 计数 + user_version 三重锁；FK 每连接开；文件库 WAL；重开幂等）。
- `orchestrator/__init__.py` — 修改：模块地图加 database.py。
- `tests/conftest.py` — 修改：加 seed_minimal（最小合法因果图，走完一次 I3 关问）+ seeded_conn 夹具。
- `tests/test_database.py` — 新增：建库/三重锁用例（checksum 漂移 fresh+reopen / 计数 / 版本 / FK 实际生效 / WAL / 重开幂等 / ANALYZE 不误判 / 提取保真守卫）。
- `tests/test_invariants.py` — 新增：I1–I6 + append-only + evidence/evaluation/question 状态机否定用例（含 child_answer 子域 3 例、身份冻结、I2 三检、evidence 多态/一致性；多守卫例用消息断言钉死目标触发器）。
- `tests/test_v23_tables.py` — 新增：external_candidate/license_review/external_import/execution_log/observation/interaction_* DB 约束否定用例（append-only/provenance/license/恰一 owner 双向/一 goal 一 pending/codex 不写机器事实/directive provenance）。

## Review（codex-chatgpt gpt-5.5/xhigh + 内审 Opus 子代理）
- **内审（Opus 子代理，落提交前）**：REQUEST_CHANGES（无 BLOCKER）——SHOULD-1 directive-CHECK 测错触发器（实撞 provenance 触发器）、SHOULD-2/3 I3+interaction append-only 覆盖缺口、NIT-4/5/6。全部采纳修复（补 child_answer 子域/身份冻结/一致性用例、iclass·ireply append-only、终态用例改用 resolution_json 隔离、database reopen 注释）。
- **codex 第 1 轮**：REQUEST_CHANGES——1 BLOCKER（reopen 不校 checksum，字节冻结主路径失效）+ 4 SHOULD（sqlite_stat1 误计 table / no-rollback 与 closed-goalver 两用例撞错守卫 / execlog 单向 owner）+ 1 NIT（README 三重披露非 DB 不变量）。全部采纳：checksum 移出 fresh 分支每次校验、计数排除 sqlite_%、两用例改隔离+消息断言、补双 owner 用例、README 补澄清。
- **codex 第 2 轮**：APPROVE（无 BLOCKER/SHOULD/NIT）——确认第 1 轮问题实质关闭。
- 未采纳意见：无。

## 验证
- 命令：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`
- 关键输出：
  ```
  198 passed in ~2s
  ```
  （M0 基线 107 + CP2.1 新增 91；全部否定用例经 diag 脚本核验命中**预期**触发器/CHECK，非偶然撞别约束。）
- DDL 保真：`diff <(sed -n '918,1614p' reference/第一部分) <(tail -n +5 db/migrations/0001_appendix_a.sql)` = 空（697 行字节一致）。
- 步级验证：本检查点未收尾步②（M1a 还差 Gate 半 = CP2.2）。
- 结论：**通过**。

## 遗留 / 回退
- 待办：CP2.2（Gate 三级校验换真 + gate_input authorizer 隔离拒读 + gate_close_question 业务门禁 + 应用层否定用例）；
  CP2.3（StateStore→SQLite + decompose 原子性 kill-9 无半写）；CP2.4（M1–M3 隔离用例）。
- 架构指针（§6.6）：StateStore 与 gate_* **共用同一 WriteDaemon 写服务 / 事务边界** → CP2.2/2.3 引入单写 WriteDaemon；
  裁量：M1 把 Gate/StateStore 做成真实组件并单元/否定/隔离测试，M0 driver 端到端切真 loop 归 M3 Advancer（细节待 CP2.2 精读后定，落受审载体）。CP2.2 参考速查见开发期 scratchpad（已摘 §6.13/§4.1.4）。
- 回退：`git revert 6d45b53`（database.py + tests + db/ 均新增，无对既有模块行为改动，回退安全）。
