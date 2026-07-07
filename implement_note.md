# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-07 21:30 ｜ 位置：步② M1 · CP2.1 已提交，CP2.2 待开工
- 检查点状态：空闲（CP2.1 记账完成；CP2.2 未开工）

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**CP2.1 已完成并记账**（commit 6d45b53，build_log 0010）：冻结 Appendix-A DDL 落库 + database.py 三重锁 +
91 条 DB 层不变量否定用例（I1–I6/append-only/v2.3-2.4 表约束）。codex 第2轮 APPROVE，pytest 198 passed。
三 OPEN 裁定已落 db/README.md（受审载体）。

## 工作区状态
- 干净（CP2.1 检查点提交 6d45b53 + 本记账提交后）。
- CP2.2 参考速查已备：开发期 scratchpad `CP2.2_refsheet.md`（§6.13 WriteDaemon/authorizer + §4.1.4 16 个 gate_* + §6.6/§6.10）；
  `M1_refsheet.md`（I1–I6/gate_close_question/decompose/v2.3-2.4 表约束精摘）。
  ⚠️ scratchpad 是 session 临时目录，换 session 会丢——新 session 需时重跑 Explore 提取（reference §6.13/§4.1.4）。

## M1 检查点计划（步②，边走边定；细节见 ROADMAP）
- [x] **CP2.1（M1a-DB）** 冻结 schema + DB 层否定用例 — 6d45b53 / build_log 0010
- [ ] **CP2.2（M1a-Gate）** Gate 三级校验换真：单写 WriteDaemon + Gate 受限只读连接（authorizer deny 9 表）+ gate_input_* 视图 + level-3 业务门禁 gate_close_question + 应用层否定用例（authorizer 拒读 v_metric_result_trajectory/observation/interaction_request；gate_close_question 拒 target_complete/applicability 同版负向）
- [ ] **CP2.3（M1b）** StateStore→SQLite（共用 WriteDaemon）+ decompose 单事务原子性（kill-9 无半写）
- [ ] **CP2.4（M1c）** v2.3/v2.4 表隔离行为 + M1–M3 隔离拒绝用例

## 下一步动作（按序，具体到命令/文件）
1. 开 CP2.2：先定**裁量**——WriteDaemon 先落还是随 Gate？（倾向：CP2.2 引入 WriteDaemon 骨架 + Gate 受限连接/authorizer/gate_input_* 视图 + gate_close_question；池注册 gate_*(baseline/variant/eval…) 体量大，可能拆到 CP2.2b 或并入 CP2.3 前）。精读 reference §4.1.4 附注（结果评审 subject_hash 判据）后定检查点边界，落 implement_note + ROADMAP。
2. 关键设计点：gate_input_* 视图集（不含 9 禁表）+ SQLite authorizer 回调（sqlite3 set_authorizer）deny 9 表 SELECT；三级校验第③级换真（业务门禁只从 gate_input_* 取数）。
3. 照 §5 循环：内审 Opus 子代理 + codex 外审 ≤2 轮 → commit → build_log 0011。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体（db/README.md 或对应制品）+ 记 build_log。
- **codex 外审用模式 B**（settings.local.json 无 codexro-review 放行，agent 不能自加）：
  `env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`
  必须**后台跑**（Bash 工具默认 120s 超时会杀掉；xhigh 审 1600 行需数分钟）+ prompt 声明「全部内联、无需执行命令」。proxy 必带（codex-chatgpt 会 unset 代理）。
- DDL 是字节冻结对象：改 schema 要同步更新 database.py 的 MIGRATION_SHA256（decisional，走评审）。
- 否定用例纪律：多守卫可能都拒同一写时，用 `_raises_msg`（消息断言）钉死命中目标触发器；写完用 diag 脚本核每例命中预期约束。
- 测试基线现 198；端到端 `scripts/run_m0_acceptance.py --cycles 5`（花真 token）。
- prompt 工程铁律（M0 实测）：给工人字段说明逐字键名+JSON骨架；oneOf 校验错误必须展平子错误。
