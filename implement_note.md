# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 ｜ 位置：步⑦（M6）CP7.2
- 检查点状态：自验通过（510 绿=502+8）→ 内审/外审中。改动=stage_provider.py 新建（StageProvider：真
  CodexRunner→(cyc,pack)→files 阶段回调 idea/plan/reasoning；render 由调用方、run+信封解析+逐产物 schema
  校验+artifact_parse 重试[附错误反馈]；与真 SqliteAdvancer 端到端 mock 跑通）+ test_stage_provider.py(8)。
  **CP7.2 收窄为 StageProvider 适配器**（judge provider + 全系统入口 run.py 移 CP7.3；attack 场景 CP7.4）。

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**M0–M5 全完成；M6 进行中**：CP7.1（da38b2f 长跑自终止安全网 §4.4.6 τ）已完成并记账。测试基线 **502 绿**。
M6 refsheet 在 scratchpad/M6_refsheet.md（含 §7.3 机制剧本 4 场景 + §7.4 T1/T2 + OPEN #4 + τ 三停）。
**建造/执行边界**（已裁决入 ROADMAP）：M6 建造=让系统「完整运行进入全自动」的机器（自终止✓ + 真 Codex
装配 + 双模式 + §7.3 机制集成验收）；§7.4 T1/T2 真跑（数百轮×24h 真 EEG）属运维执行、非本轮建造。

**CP7.2 目标**（真 Codex 生产装配——让系统真能全自动跑起来）：
1. **StageProvider 适配器**（新）：把 CodexRunner 包成 advancer/attack_stages 消费的 provider 回调——
   compiler.render(cycle,stage) → runner.run_task(system_prompt, skill, pack) → 解析信封 {files, md} →
   逐产物 schema 校验（artifact_parse 重试 ≤2，§4.2.3）→ 返回 files dict。参考 driver.py:405/450 的
   真 Codex 调用+解析逻辑（那是 M0 桩栈版，本次绑真组件）。system_prompt/skill 取 prompts/ + SKILL.md。
2. **全系统装配入口 run.py**（新）：goalbrief.parse → database.connect+建库 → 装配 WriteDaemon/
   SQLiteStateStore/SqliteCompiler/SqliteGate/AttackStages/ImportWorker/Console/Mediator/notify/
   StatusPublisher/StopController → SqliteAdvancer(全部注入) → run_cycles。CLI：目标书路径 + 工作目录。
3. **kill-9 真栈恢复冒烟**：装配入口在真组件上跑几轮 + 中途杀 + 重启续跑终库一致（复用 M3 范式）。
   真 Codex 冒烟可选（1 轮 bootstrap，环境允许时），确定性验证用 mock provider。

## 步⑦（M6）CP7.2 下一步动作（按序）
1. 精读 driver.py（M0 真 Codex 调用+信封解析+schema 校验+失败语义 §4.2.3）+ interfaces.py Runner/
   provider 签名 + attack_stages 的 p dict 期望 + prompts/system_prompt.md + 四阶段 SKILL.md 路径。
2. 写 orchestrator/stage_provider.py（StageProvider：render→run→parse→validate→files；四阶段+reasoning
   +attack idea/plan/bundle 全覆盖；artifact_parse 重试）。
3. 写 run.py（装配入口）。测试：mock runner 端到端装配跑通 + kill-9 恢复 + schema 校验失败重试。
4. §5 循环：内审 Opus → codex ≤2 轮 → commit → build_log 0030。

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
