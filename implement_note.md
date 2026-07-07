# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 00:45 ｜ 位置：步② M1 · CP2.3 已提交，CP2.4（M1c）待开工
- 检查点状态：空闲（CP2.3 记账完成；CP2.4 未开工）

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**M1 进度**：CP2.1（6d45b53，M1a-DB）+ CP2.2（be84a90，M1b StateStore/WriteDaemon）+ **CP2.3（d07c6c6，
M1a-Gate：SqliteGate authorizer 隔离 + gate_input 视图 + gate_close_question）** 已完成记账。pytest 255 passed。
**下一步 CP2.4（M1c）收尾 M1**。

## 关键裁量：M1 收尾范围（全自动自主裁决，落 ROADMAP + build_log 0012）
- **M1 acceptance（§7.1）= CP2.1（DDL+DB负例）+ CP2.2（StateStore）+ CP2.3（门禁+三级校验+gate_close_question 负例）+ CP2.4（M1c 隔离）**。
- **池注册 gate_*（baseline/variant/evaluation/attempt/run/build_target… §4.1.4 其余 15 个）不在 M1 acceptance**——
  它们是 bundle 事实写入路径，**M1–M3 假执行不产真池对象**；随 **M3 Advancer 跑真 loop**（或 M4 真执行）需要时补。
  M1c 隔离用例正是证「假执行期这些真池对象/target 不被产生」。
- parser_suspect=M4；reasoning-finish 全序原子（gate 关问+tree_ops+selection 一事务）=M3。

## 工作区状态
- 干净（CP2.3 检查点提交 d07c6c6 + 本记账提交后）。
- 真实资产层组件（未接 driver，M3 Advancer 接）：database.py / writedaemon.py / statestore_sqlite.py / gate_sqlite.py。
- 参考速查（开发期 scratchpad，换 session 会丢）：M1_refsheet.md、CP2.2_refsheet.md（§6.13/§4.1.4 16 gate_*）。

## CP2.4（M1c）目标
「M1–M3 隔离拒绝用例」（§7.1 M1 行 + 第三部分「M1–M3 隔离拒绝用例」）：证 v2.3/v2.4 子系统（import/interaction）
在假执行期不破 M0–M3 边界——
① external_candidate/选择**不产** executable target、不入 baseline pool、不参与 evidence/report（断言查无此类行）；
② 入站消息**不触发**真 query responder、不改 route/decision/question；pending interaction_request 不触发真通知/全局等待（桩）；
③ import 分支恒 deferred（占位 baseline(planned) + pending question_dep；真物化只 M4）。
→ 这些多为**断言 + 桩**（子系统本身 M4/M5 才落真），本轮证「不越界」。需精读第三部分 §7.1「M1–M3 隔离拒绝用例」段 + 第一部分 §3.6.3/§4.2.1/§4.6.2。

## 下一步动作（按序）
1. 精读第三部分「M1–M3 隔离拒绝用例」+ 第一部分 §3.6.3（import deferred 三写入）/§4.2.1（pending dep 排除调度）/§4.6.2（分类 unclear）。
2. 裁量 CP2.4 落地形态：多为「桩子系统 + 隔离断言」（import register 只登记不物化；interaction 入站 durable 但不触发真 responder）。可能需薄桩 importer/interaction 写入 + 断言。定检查点边界落 implement_note+ROADMAP。
3. 照 §5 循环：内审 Opus + codex 外审 ≤2 轮 → commit → build_log 0013 → **收尾步② M1**（跑 M1 步级验证：I1–I6 + v2.3/v2.4 否定 + decompose kill-9 + 隔离用例全过）。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体 + 记 build_log。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。
- **评审很能抓真 bug**（CP2.1–2.3 共 codex 8 BLOCKER + 内审 3 BLOCKER）。写资产层务必：类型前缀 id 解码、进程内投影随事务回滚、护栏取策略键名累计口径、可恢复字段落库、gate 拒因不撞 FK（NULL+payload）、gate-only 判据写锁内重跑防 TOCTOU、except 只收 IntegrityError、门禁读连接 mode=ro。
- **否定用例纪律**：多守卫可能都拒同一写 → 消息断言（match=）钉死目标；写完 diag 核每例命中预期。
- gate 测试须**文件库**（门禁 mode=ro 独立连接；:memory: 每连接独立库）。
- DDL 字节冻结：改 schema 同步 database.py 的 MIGRATION_SHA256（走评审）。
- 测试基线现 255；端到端 `scripts/run_m0_acceptance.py --cycles 5`（花真 token）。
- 悬案（build_log 0011/0012）：resolve_deps dead_end 依赖（保 M0 语义，M3/M6 定）；跨版 child_answer applicability（现 try/except 兜底，M3 goal-amend 验）。
