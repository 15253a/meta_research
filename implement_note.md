# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-07 23:10 ｜ 位置：步② M1 · CP2.2 已提交，CP2.3 待开工
- 检查点状态：空闲（CP2.2 记账完成；CP2.3 未开工）

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**M1 进度**：CP2.1（6d45b53，M1a-DB：冻结 schema + DB 层否定用例）+ **CP2.2（be84a90，M1b：单写 WriteDaemon
+ SQLiteStateStore + decompose 原子性 + kill-9 无半写）** 已完成并记账。pytest 233 passed。

## 工作区状态
- 干净（CP2.2 检查点提交 be84a90 + 本记账提交后）。
- 新增未接 driver 的真实组件：`orchestrator/writedaemon.py`、`orchestrator/statestore_sqlite.py`（M3 Advancer 才接）。
- CP2.3 参考速查：开发期 scratchpad `CP2.2_refsheet.md`（§6.13 WriteDaemon/authorizer + §4.1.4 16 个 gate_*）+ `M1_refsheet.md`。⚠️ scratchpad 换 session 会丢，需时重跑 Explore 提取。

## M1 检查点计划（CP 编号已 swap：StateStore 先于 Gate）
- [x] **CP2.1（M1a-DB）** 冻结 schema + DB 层否定用例 — 6d45b53 / 0010
- [x] **CP2.2（M1b）** WriteDaemon + SQLiteStateStore + decompose 原子性 — be84a90 / 0011
- [ ] **CP2.3（M1a-Gate）** Gate 三级校验换真：Gate 受限只读连接 + SQLite authorizer(deny 9 表 SELECT) + gate_input_* 视图（**TEMP 视图**：不进 36/72/29/1 冻结计数）+ level-3 业务门禁 gate_close_question + 应用层否定用例（authorizer 拒读 v_metric_result_trajectory/observation/interaction_request；gate_close_question 拒 target_complete/applicability 同版负向）
- [ ] **CP2.4（M1c）** v2.3/v2.4 表隔离行为 + M1–M3 隔离拒绝用例

## 下一步动作（按序，具体到命令/文件）
1. 开 CP2.3（Gate 换真）。**关键设计点**：
   - Gate 受限只读连接：`sqlite3.Connection.set_authorizer(cb)`，cb 对 9 禁表（execution_log/execution_observation/interaction_message/classification/reply/request/external_candidate/external_import/license_review）的 SELECT 返回 `SQLITE_DENY`；其余放行。
   - gate_input_* 视图集：因 36/72/29/1 是冻结计数、加永久视图会破锁 → 用 **TEMP VIEW**（建在 Gate 连接的 temp schema，不进 sqlite_master 主计数）；或 Gate 直接查允许的基表（authorizer 兜底）。裁量落 build_log。
   - gate_close_question（§4.1.4 全套 I3 判据，见 M1_refsheet）：写 answer+evidence + question→answered/refuted，含 target_complete / applicability 同版负向 / parser_result_suspect 判据。经 WriteDaemon（与 StateStore 共用，§6.6）。
   - 池注册 gate_*（baseline/variant/eval/attempt…，§4.1.4 16 个）体量大——**裁量是否本检查点全落还是拆 CP2.3b**（精读 §4.1.4 附注「结果评审 subject_hash」后定，落 implement_note+ROADMAP）。
2. 照 §5 循环：内审 Opus 子代理 + codex 外审 ≤2 轮 → commit → build_log 0012。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体 + 记 build_log。
- **codex 外审用模式 B 后台跑**：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」；Bash 工具 120s 会杀→必须 run_in_background。
- **评审很能抓真 bug**：CP2.2 内审 1 BLOCKER + codex 两轮共 4 BLOCKER（投影回滚 / 类型前缀 id / open 数 +1 / active_question_id 落库 / revalidate 累计绕过）。写状态机 SQL 务必：类型前缀解码、进程内投影随事务回滚、护栏用策略键名口径（累计非 per-op）、可恢复字段落库。
- **否定用例纪律**：多守卫可能都拒同一写 → 用消息断言（match=）钉死目标触发器；写完 diag 核每例命中预期约束。
- DDL 字节冻结：改 schema 要同步 database.py 的 MIGRATION_SHA256（走评审）。
- 测试基线现 233；端到端 `scripts/run_m0_acceptance.py --cycles 5`（花真 token）。
- 未决悬案（记 build_log 0011）：resolve_deps 是否把 dead_end 子问题当满足父依赖——保持 M0 语义，留 M3/M6 按规格定。
