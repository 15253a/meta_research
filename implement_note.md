# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 ｜ 位置：步③（M2）· CP3.2 已提交，CP3.3 待开工
- 检查点状态：空闲（CP3.2 记账完成；CP3.3 未开工）

## M2 检查点计划（步③）
- [x] **CP3.1** SqliteCompiler：DB→确定性四区包（字节一致 diff=0；render 单读快照 + ORDER BY 定序 + json sort_keys + budget float + pack_hash \x00 含 refs）+ applicability 六枚举徽标 + _open_set 限 goal_id 含本轮 Qn + manifest 纯函数（sources 落 ContextPack）— commit 2110f02 / build_log 0014
- [x] **CP3.2** Recall 四级可停（§3.6.2：卡片 LIKE+faceted tag→变体矩阵→测量索引→ctx-fetch）+ **复用判定 O(1) selector（§4.1.5 规范 SQL）+ EXPLAIN 证走测量索引（e 走 sqlite_autoindex_evaluation_1、mr 走 uq_mr_agg、无 SCAN e/ea/mr）** + Ctx 深潜（isascii+isdigit 双护）— commit 1a099fc / build_log 0015
- [ ] **CP3.3** 观测摘要段进 reasoning 锚点（从 execution_observation 渲 nan/loss_trend…，替 CP3.1 占位 compiler_sqlite.py:98）+ status_card 发布（§4.6.6 封闭字段）+ authorizer 拒读负例（gate 读 observation 被拒，CP2.3 已焊、此处证「摘要进 pack 但不进 gate_input」）+ import deferred 不产 target 断言 —— **收尾步③ M2**。reference：`<scratchpad>/CP3.3_refsheet.md`（换 session 需重跑 Explore）
- 架构同 M1：M2 交付真实 DB-backed 组件（读 DB），M0 driver 仍走桩、基线绿；M3 Advancer 接。SqliteCompiler 只读连接 mode=ro 由 M3 调用方传入。嵌入 defer（无模型，用 card_md LIKE + baseline_tag + 测量索引）。

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**步③（M2）推进中**：CP3.1（2110f02 编译器四区包）+ CP3.2（1a099fc Recall+复用 O(1)）已提交，均 codex APPROVE。
**下一步 CP3.3 收尾 M2**（见下「下一步动作」）。测试基线 298 绿。
**已完成**：步①（M0）+ 步②（M1）CP2.1–2.4 全绿（267）+ 步③ CP3.1/CP3.2。

## M1 交付的真实资产层（未接 driver；M3 Advancer 才接，M0 driver 仍走桩、基线保持绿）
- `orchestrator/database.py`：附录 A 冻结 DDL 建库 + checksum/计数/版本三重锁。
- `orchestrator/writedaemon.py`：单写连接 + 短事务（BEGIN IMMEDIATE，不可嵌套，鲁棒回滚）。
- `orchestrator/statestore_sqlite.py`：SQLiteStateStore（状态机 + decompose 单事务原子 + kill-9 无半写 + 类型前缀 id + active_question_id 落库 + 投影随事务回滚 + atomic() 供 M3 裹全序）。
- `orchestrator/gate_sqlite.py`：SqliteGate（authorizer mode=ro 拒 9 表 + gate_input_* TEMP 视图 + gate_close_question：gate-only 判据写锁内重跑防 TOCTOU + 触发器 ABORT→干净拒 + _reject FK-null）。
- `orchestrator/importer.py` / `interaction.py` / `ids.py`：M1c 桩（import 发现+登记 deferred / 人机 durable 入站 + 隔离）。

## 步③（M2）目标（§7.1 M2 行）
上下文编译器 + 召回 + 运行观测摘要段 + status_card 发布。验收（可证伪）：
- **同快照+配方+预算 → context_pack 字节一致（diff=0）**（确定性四区包 §4.5.1：①固定锚 ②结构邻域 ③检索区 ④引用区）。
- 召回四级可停（§3.6.2 渐进召回）。
- **复用判定 O(1)**：命中走测量索引 `(variant_id,protocol_id,protocol_ver)`，`EXPLAIN QUERY PLAN` 证明用索引、非全表扫。
- 运行观测摘要进锚点，而**门禁 authorizer 拒读**（负例——CP2.3 authorizer 已拒 execution_observation，此处证摘要进 pack 但不进 gate 判据）。
- import 仍 deferred（占位 baseline + pending dep）、**不产 target**。
- 桩→真：Compiler / Ctx / Recall（M0 固定模板/假数据 → M2 换真：FTS5+嵌入+测量索引）。

## 下一步动作（按序）—— CP3.3（收尾 M2）
CP3.3 规格全提取见 `<scratchpad>/CP3.3_refsheet.md`（字段清单 + 铁律 + 当前占位行号）。四件事：
1. **观测摘要段真渲染**：替 `compiler_sqlite.py:98` 占位。reasoning stage 固定锚从 `execution_observation`
   渲 nan_seen/divergence_flag/oom/warning/retry/last_loss/loss_trend/wall_clock_sec（append-only；source∈parser/codex，
   codex 只写 digest）。确定性纪律：ORDER BY 定序、无 wall-clock/随机、纳入 pack_hash。**铁律**：观测只影响调试/复现/
   下一步评估，不得作 novelty/success/correctness/关问题选择输入（防绕过门禁）。
2. **status_card 发布**（§4.6.6 封闭字段，prose 契约、非核心 DDL）：snapshot_cycle/goal 版本摘要/active question 卡/
   cycle.status·route/selection intent+最近 selection DECISION 摘要/预算(B(t)·已花·剩余)/open·inconclusive 计数/
   heartbeat ref/pending 文件请求摘要。M2 先做发布函数（真 advance 接入=M3）。呈现已关闭结论处须 join answer_applicability 徽标。
3. **authorizer 拒读负例**：证观测摘要进 pack（编译器普通只读连接读到 execution_observation），但 gate authorizer
   拒读 execution_observation（CP2.3 SqliteGate 已焊 9 表 deny；此处加测试证「摘要进 pack 但不进 gate_input」分离）。
4. **import deferred 不产 target 断言**：import 三写入（external_import selected + 占位 baseline planned + question_dep pending）
   跑完后，build_target 表无新增行（M1–M3 deferred，M4 才物化产 run.kind=import）。DeferredImporter（importer.py）已实现三写入，此处加断言测试。
5. 照 §5 循环：内审 Opus 子代理 + codex 外审 ≤2 轮 → commit → build_log 0016 → 跑步③ M2 步级验证（§7.1 M2 全项）收尾步③。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体（对应制品/build_log）。M2 无 OPEN 阻塞（OPEN #1/#2 已 M1 裁）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；APPROVE 才 commit。
- **评审极能抓真 bug**（M1 期 codex 共 ~10 BLOCKER + 内审 ~4 BLOCKER）。资产层务必：类型前缀 id 解码（ids.py）、进程内投影随事务回滚、护栏取策略键名累计口径、可恢复字段落库、gate 拒不撞 FK、gate-only 判据写锁内重跑、except 只收 IntegrityError、门禁读连接 mode=ro、写子系统前置一致性校验（candidate↔question 等）、幂等锚正确。
- **确定性纪律（M2 核心）**：context_pack 字节一致——无 wall-clock/随机/dict 无序；ORDER BY 定序；manifest 溯源。
- gate/文件库测试须**文件库**（门禁 mode=ro 独立连接）；importer/interaction/statestore 可 :memory:（单 WriteDaemon 连接）。
- DDL 字节冻结：改 schema 同步 database.py 的 MIGRATION_SHA256（走评审）。
- 测试基线现 **298**（M0/M1 267 + CP3.1 16 + CP3.2 15）；端到端 `scripts/run_m0_acceptance.py --cycles 5`（花真 token，走 M0 桩栈）。
- 悬案（build_log 0011/0012）：resolve_deps dead_end 依赖（保 M0 语义，M3/M6 定）；跨版 child_answer applicability（现 try/except 兜底，M3 goal-amend 验）。
