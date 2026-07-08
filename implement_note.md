# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 ｜ 位置：步④（M3）· CP4.1 已提交，CP4.2 待开工
- 检查点状态：空闲（CP4.1 记账完成；CP4.2 未开工）。测试基线 333 绿。

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**步③（M2）已完成**：CP3.1（2110f02 编译器确定性四区包）+ CP3.2（1a099fc Recall 四级+复用 O(1)）+ CP3.3
（72647f8 观测摘要进锚点+status_card+authorizer 负例）。§7.1 M2 五判据步级验证全过（见 build_log 0016）。
**步④（M3）进行中**：CP4.1（148c907 Advancer 骨架：derive_next_route 全矩阵 + advance bootstrap 创世轮，
真 SQLite 单一 atomic 阶段 + 续跑幂等）已提交。**下一步 CP4.2**。
**已完成**：步①（M0）+ 步②（M1）+ 步③（M2）+ 步④ CP4.1。测试基线 **333 绿**。

## 步④（M3）目标（§7.1 M3 行）——编排器 Advancer + 恢复 + import 降级
Advancer 把 M1/M2 真实组件接进循环（现 M0 driver 仍走桩）。验收（可证伪）：
- **恢复性**：任意阶段 kill -9 重启续跑，终库状态与不杀一致（排除非确定字段）。阶段边界即检查点（P6，§4.4.5）。
- **import deferred 隔离**：pending dep 排除调度、不重复登记、不产 target（部分已由 test_isolation_m1c 覆盖，M3 补全链）。
- 开工前确认 OPEN #3（全自动：自主裁决 + 落受审载体，勿停问用户）。

## M1/M2 已交付的真实组件（未接 driver；M3 Advancer 才接，M0 driver 仍走桩、基线绿）
- `orchestrator/database.py`：附录 A 冻结 DDL + checksum/计数/版本三重锁。
- `orchestrator/writedaemon.py`：单写连接 + 短事务（BEGIN IMMEDIATE，不可嵌套，鲁棒回滚）。
- `orchestrator/statestore_sqlite.py`：SQLiteStateStore（状态机 + decompose 单事务原子 + kill-9 无半写 + 类型前缀 id
  + active_question_id 落库 + 投影随事务回滚 + **atomic() 供 M3 裹全序**）。
- `orchestrator/gate_sqlite.py`：SqliteGate（authorizer mode=ro 拒 9 表 + gate_input_* TEMP 视图 + gate_close_question
  gate-only 判据写锁内重跑防 TOCTOU）。**注**：池注册类 gate_*（15 函数）M1 未做，M3 需补（见 build_log 0013）。
- `orchestrator/importer.py`/`interaction.py`/`ids.py`：M1c（import 发现+登记 deferred / 人机 durable 入站 + 隔离）。
- `orchestrator/compiler_sqlite.py`：SqliteCompiler（确定性四区包 + applicability 徽标 + 观测摘要段）。
  **M3 接线点**：render 的 retrieval/refs 现留空 → 按 policy 配方调 recall_sqlite 填检索区/引用区。
- `orchestrator/recall_sqlite.py`：SqliteRecall/SqliteCtx + 复用判定 O(1) selector（四级召回）。
- `orchestrator/status_card.py`：build_status_card（§4.6.6 封闭字段）。**M3 接线点**：advance 阶段边界原子发布 +
  写 outbox；填 selection.latest_decision（按 cycle scope 查）/ global_remaining / heartbeat_ref。
- `orchestrator/budgeting.py`：compute_budget（B(t) 唯一定义）。

## 下一步动作（按序）—— CP4.2（外层驱动循环 + decompose/attack + kill-9 恢复）
M3 规格全提取见 `<scratchpad>/M3_refsheet.md`（换 session 需重跑 Explore）。CP4.1 已交付 derive_next_route + bootstrap advance。CP4.2：
1. **外层驱动循环**：open_or_resume_cycle→set_route（首轮 bootstrap，其后 derive_next_route）→激活目标问题→loop advance() until done→从 selection 派下轮 route/问题（对齐 M0 driver.run_cycles 但用真组件+Advancer；注入确定性 provider）。
2. **decompose/attack advance**：_bootstrap_cycle 之外加 decompose（activate 目标+add_children）与 attack 骨架（idea/plan 用 cycle.status 作 stage cursor + phase_commit 幂等；池注册/真执行留 M4）。
3. **kill-9 恢复测试**（M3 核心验收）：subprocess 阶段边界杀进程，重启续跑终库与不杀一致（**比较排除 timestamp/attempt_id/log offset**，见 advancer.py docstring）。对齐 statestore kill-9 子进程测试范式。
4. 每检查点照 §5 循环。CP4.3 = import deferred 隔离全链（收尾 M3，跑 §7.1 M3 步级验证）。
   精读指针（存档）：第一部分 §4.2/§4.4/§3.6.3/§4.2.4-5；第二部分 §6.7/§6.11/§6.13；M0 `orchestrator/driver.py` 作接入范式。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体（对应制品/build_log）。M3 有 OPEN #3 待自主裁决。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；APPROVE 才 commit。
- **评审极能抓真 bug**（M1 期 codex ~10 BLOCKER + 内审 ~4；M2 期 codex/内审共 ~6 SHOULD 全采纳）。资产层务必：类型前缀 id 解码（ids.py）、进程内投影随事务回滚、可恢复字段落库、gate 拒不撞 FK、gate-only 判据写锁内重跑、门禁读连接 mode=ro、写子系统前置一致性校验、幂等锚正确。
- **确定性纪律（M2 核心，M3 续守）**：context_pack 字节一致——无 wall-clock（含 DB created_at）/随机/dict 无序；ORDER BY 定序。
- gate/文件库测试须**文件库**（门禁 mode=ro 独立连接）；importer/interaction/statestore/compiler/status_card 可 :memory:。
- DDL 字节冻结：改 schema 同步 database.py 的 MIGRATION_SHA256（走评审）。
- 测试基线现 **316**；端到端 `scripts/run_m0_acceptance.py --cycles 5`（花真 token，走 M0 桩栈）。
- 悬案（build_log 0011/0012）：resolve_deps dead_end 依赖（保 M0 语义，M3/M6 定）；跨版 child_answer applicability
  （现 try/except 兜底，M3 goal-amend 验）。status_card selection.latest_decision / global_remaining / heartbeat_ref
  与 compiler 检索区/引用区接线 = M3（见 build_log 0016 遗留）。
