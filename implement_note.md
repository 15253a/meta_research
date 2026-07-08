# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 ｜ 位置：步⑦（M6）待开工（步⑥ M5 已完成）
- 检查点状态：空闲（M5 收尾记账完成；M6 未开工）

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**步⑥（M5）已完成**：CP6.1（702071e 保守分类器+directive 生命周期）6.2（28c6117 query 只读应答链+
status_card 发布）6.3（bee3b9e 通知矩阵 outbox+文件请求全流水+全局等待）。§7.1 M5 步级验证过
（build_log 0028，64 测联合勾兑）。**已完成：M0–M5 全部**。测试基线 **489 绿**。
**下一步 步⑦（M6）**：长跑 + 验收剧本——数百轮无人值守不漂移；双模式 A/B 实测定默认；§7.3 机制剧本
（happy+失败路径）；§7.4 研究能力任务 T1/T2。**开工前确认 OPEN #4（paper-gap 谓词）**——全自动模式：
自主裁决 + 落受审载体 + build_log 记录。

## 步⑦（M6）开工序（下一 session 从这里接）
1. **精读 M6 规格**：reference/ 第三部分 §7.1 M6 行原文 + §7.3 机制验收剧本 + §7.4 研究能力任务
   （T1/T2）+ OPEN.md #4（paper-gap 谓词）。建议 Explore 提取到 scratchpad refsheet 省上下文。
2. **OPEN #4 自主裁决**（全自动）：paper-gap 谓词定义落受审制品（goal_brief/policy/schema 视规格），
   build_log 记裁决理由。
3. **裁量 M6 检查点切分**（落 ROADMAP+本文件）。粗切设想：CP7.1 长跑 harness（数百轮驱动脚本+漂移
   断言+资源看护）；CP7.2 §7.3 机制剧本逐条（happy+失败路径）；CP7.3 §7.4 T1/T2 研究任务 + 双模式
   A/B + 硬化清单选做；按需增减。
4. 照 §5 循环：内审 Opus 子代理 + codex 外审 ≤2 轮 → commit → build_log 0029 起。

## 已交付组件全景（M0–M5；M0 driver 走桩栈并存、基线绿）
- 存储/守卫：database（冻结 DDL 三重锁）/writedaemon（单写短事务）/statestore_sqlite/gate_sqlite
  （authorizer+close_question）/gate_exec（九 gates+review_passed）/gate_pool（claim/register）/
  phase_commit。
- 证据/观测：harness（真子进程+staging 原子+exit 侧车）/obs_parser（确定性+suspect 真派生）/
  subject_manifest。
- 循环：advancer（run_cycles+恢复+worker 轮识别+**status_publisher 每格发布+precheck 全局等待**）/
  attack_stages（idea/plan/bundle/reasoning 全链）/import_worker（物化全链）。
- 检索/编译：compiler_sqlite/recall_sqlite/budgeting/status_card（latest_decision cycle 作用域+
  SqliteStatusPublisher 原子发布）。
- 人机（M5 新齐）：interaction（durable 入站/ack/文件请求单）/console（保守分类+directive 生命周期：
  润色≠raw/回显确认/单事务消费/pause 按消费序阻断）/mediator（mode=ro+全写拒/grounding/模板应答/
  重建一致）/notify（outbox 幂等文件队列/7 态+3 事件扫描派生/FileRequestService 全流水/
  make_advancer_precheck）。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；APPROVE 才 commit；**外审 diff 排除记账类**（`':(exclude)build_log/**' ':(exclude)implement_note.md'`）。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- **M5 已定语义**（评审沉淀，别改回去）：pause 阻断=最近被消费的 pause/resume 是 pause（消费序；
  pending 不阻断→前置检查先消费再查）；pending_directives 只出软/已确认硬；查询文本按 message_id
  从持久层取；实体 grounding 环视+大小写不敏感；temp schema 在 mode=ro 下仍可写（authorizer 拒
  TEMP*/VTABLE）；outbox committed=换行终止（无尾换行段=未入队）；文件请求幂等先于 quota、
  provenance 同 goal、symlink 全链不跟、hash 锚 copy 后 dest 字节；reminder 只发当前档；
  reasoning_start 消费时机=attack 游标 'bundle'（下一格才是 reasoning）。
- gate/mediator/发布器/notify-advancer 集成测试须**文件库**；statestore/console/notify 单元可 :memory:。
- DDL 字节冻结：outbox/状态卡=实现层文件勿建表；改 schema 须同步 MIGRATION_SHA256（走评审）。
- 测试基线现 **489**（M5 联合勾兑子集 64）。
- 悬案/M6 硬化清单（0023–0028 累积）：注册段合一事务、bundle pc 产物集哈希锚、route plan 后特化
  （eval_only/reuse_only/dependency_wait）、attack/import 注册骨架共享、完整供应链 manifest、修复
  重评轮数、report 制品、materialize_failed 重试、resolve_deps dead_end 悬案、compiler 检索区接
  recall、console 效果接线（set_budget/reprioritize/goal_amend 真效果+真 Codex 润色/语义分类/应答）、
  prune_branch 子树级联、多轮并发 abort、发布失败重试、p95 压测口径、outbox 轮转、真 QQ connector、
  reminder 生产驱动接 time.time()。
