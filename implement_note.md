# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 ｜ 位置：步⑥（M5）待开工（步⑤ M4 已完成）
- 检查点状态：空闲（M4 收尾记账完成；M5 未开工）

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**步⑤（M4）已完成**：CP5.1（执行 gates）5.2（注册 gates+manifest）5.3（harness+parser，OPEN#5 闭）
5.4（attack 全链——首次全链跑通）5.5（import 物化，OPEN#6 闭）5.6（语义 5 判例验收）。§7.1 M4 全判据
步级验证过（build_log 0025，47 测联合勾兑）。
**已完成：M0–M4 全部**。测试基线 **436 绿**。**下一步 步⑥（M5）人类控制台 + query 只读应答器**。
OPEN 项：#1/#2/#3/#5/#6 已闭；#4（paper-gap 谓词）留 M6 目标书定稿。

## 步⑥（M5）目标（§7.1 M5 行）
验收（可证伪）：directive 按时机消费同记 DECISION；query 只读边界负例（responder 写库全被 authorizer 拒）；
分类负例（unclear 不自动答）；ACK/query p95<2s；中介线程重建一致；润色≠raw；通知矩阵逐态推送断言；
文件请求全流水 + 负例。

## 已交付组件全景（M0–M4；M0 driver 走桩栈并存、基线绿）
- 存储/守卫：database（冻结 DDL 三重锁）/writedaemon（单写短事务）/statestore_sqlite（状态机+atomic+恢复读）/
  gate_sqlite（authorizer+close_question+parser_suspect 可选）/gate_exec（执行生命周期九 gates+review_passed）/
  gate_pool（claim/register+new_protocol）/phase_commit。
- 证据/观测：harness（真子进程+staging 原子纪律+exit 侧车）/obs_parser（确定性 parser+suspect 真派生
  [当前口径过滤+stale fail-closed+多 log OR]+content_hash 锚）/subject_manifest（双评审配方）。
- 循环：advancer（derive_next_route 全矩阵+run_cycles+reasoning-only advance+worker 轮识别）/attack_stages
  （idea/plan/bundle/reasoning 全链，两段提交+结构恢复+judge replay-safe）/import_worker（物化全链+失败全拒）。
- 检索/编译：compiler_sqlite（确定性四区包+观测摘要+徽标；检索区接线=M6 注）/recall_sqlite（四级召回+
  复用 O(1)）/status_card（§4.6.6 封闭字段；M5 接 latest_decision/heartbeat）/budgeting。
- M1c 人机雏形：interaction.py（InteractionIngest：durable 入站+模板 ACK+文件请求单，幂等；**M5 的起点**）。

## 下一步动作（按序）—— 步⑥（M5）开工
1. **精读 M5 规格**（建议 Explore 提取省上下文，scratchpad 已清）：第一部分 §4.6 全节（人机交互：4.6.1 通道/
   4.6.2 中介与重建/4.6.3 分类/4.6.4 directive 消费/4.6.5 query 应答器/4.6.6 status_card 发布+通知矩阵/
   4.6.7 启动输入/4.6.8 文件请求）；第二部分 §6.12（实现四件套）。第三部分 §7.1 M5 行原文。
2. **裁量 M5 检查点切分**（精读后定，落 implement_note+ROADMAP）。粗切设想：CP6.1 分类器+directive 消费
   （保守三分类+按时机消费+DECISION）；CP6.2 query 只读应答器（authorizer 写拒负例+grounding+模板回退+
   status_card 接线[latest_decision 按 cycle scope]）；CP6.3 中介线程重建+通知矩阵+文件请求全流水+SLA 断言收尾。
3. 照 §5 循环：内审 Opus 子代理 + codex 外审 ≤2 轮 → commit → build_log 0026 起。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；APPROVE 才 commit；**外审 diff 排除记账类**（`':(exclude)build_log/**' ':(exclude)implement_note.md'`）。
- **评审极能抓真 bug**：M4 期两层评审共抓 ~9 BLOCKER（全是崩溃缝隙/防篡/死循环类，多经实证复现）——凡涉
  恢复/幂等/评审闸，务必逐缝自查：register↔补登、final↔exit、judge fail、终态早退 pc、收尾↔resolve_deps。
- **确定性纪律**：context_pack 字节一致；恢复比较排除 timestamp/attempt_id/log offset；观测 P6 可回放
  （PARSER_VERSION+extraction_policy_hash 当前口径过滤，stale≠clean）。
- gate/文件库测试须**文件库**（authorizer 读连接独立）；statestore/importer 可 :memory:。kill-9 测试范式
  subprocess+marker+SIGKILL；崩溃缝隙可在-process 用「炸一次→新实例续跑→终库对比」模拟。
- DDL 字节冻结：改 schema 须同步 database.py MIGRATION_SHA256（走评审）。policy.yaml/schema 改动=决策性。
- 测试基线现 **436**；M4 步级验证联合勾兑 = test_m4_semantic_cases + test_import_worker + test_attack_advance
  + test_obs_parser（47 测）。
- 悬案/M6 硬化清单（build_log 0023/0024/0025 遗留汇总）：注册段合一事务、bundle pc 产物集哈希锚、route plan
  后特化（eval_only/reuse_only/dependency_wait）、attack/import 注册骨架共享、完整供应链 manifest、修复重评
  轮数、report 制品、materialize_failed 重试（按 selection_key 取下一候选）、resolve_deps dead_end 悬案、
  compiler 检索区接 recall。
