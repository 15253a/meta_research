# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 01:50 ｜ 位置：**步②（M1）已完成**；步③（M2）待开工
- 检查点状态：空闲（M1 收尾记账完成；M2 未开工）

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**步②（M1）资产层落地——完成**：CP2.1（6d45b53 M1a-DB）+ CP2.2（be84a90 M1b）+ CP2.3（d07c6c6 M1a-Gate）
+ CP2.4（1182a5a M1c）。步级验证 267 全绿（§7.1 M1 逐项过，见 build_log 0013）。**下一步 步③（M2）**。

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

## 下一步动作（按序）
1. **精读 M2 规格**（建议 Explore 提取，省上下文）：第一部分 §4.5（上下文编译器四区包/确定性契约）、§3.6.2（渐进四级召回）、§4.7（运行观测摘要）、§4.6.6（status_card）、第二部分 §6.7（编译器实现）/§6.10（Compiler/Ctx/Recall 换真行）。看现有 `orchestrator/compiler.py`（StubCompiler，M0 固定模板 + manifest 溯源）作起点。
2. **裁量 M2 检查点切分**（建议：CP3.1 Compiler 确定性四区包换真 + 字节一致；CP3.2 Recall 四级召回 + 复用判定 O(1)/EXPLAIN；CP3.3 观测摘要进锚点 + status_card 发布 + authorizer 负例）。精读后定，落 implement_note + ROADMAP。
3. 每检查点照 §5 循环：内审 Opus 子代理 + codex 外审 ≤2 轮 → commit → build_log。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体（对应制品/build_log）。M2 无 OPEN 阻塞（OPEN #1/#2 已 M1 裁）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；APPROVE 才 commit。
- **评审极能抓真 bug**（M1 期 codex 共 ~10 BLOCKER + 内审 ~4 BLOCKER）。资产层务必：类型前缀 id 解码（ids.py）、进程内投影随事务回滚、护栏取策略键名累计口径、可恢复字段落库、gate 拒不撞 FK、gate-only 判据写锁内重跑、except 只收 IntegrityError、门禁读连接 mode=ro、写子系统前置一致性校验（candidate↔question 等）、幂等锚正确。
- **确定性纪律（M2 核心）**：context_pack 字节一致——无 wall-clock/随机/dict 无序；ORDER BY 定序；manifest 溯源。
- gate/文件库测试须**文件库**（门禁 mode=ro 独立连接）；importer/interaction/statestore 可 :memory:（单 WriteDaemon 连接）。
- DDL 字节冻结：改 schema 同步 database.py 的 MIGRATION_SHA256（走评审）。
- 测试基线现 **267**；端到端 `scripts/run_m0_acceptance.py --cycles 5`（花真 token，走 M0 桩栈）。
- 悬案（build_log 0011/0012）：resolve_deps dead_end 依赖（保 M0 语义，M3/M6 定）；跨版 child_answer applicability（现 try/except 兜底，M3 goal-amend 验）。
