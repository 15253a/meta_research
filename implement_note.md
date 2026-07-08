# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 ｜ 位置：步⑦（M6）CP7.4 待开工（勘查中发现 plan 契约设计缺口，已面陈用户）
- 检查点状态：空闲。**核心里程碑已达**：系统「完整运行、进入全自动」——reasoning-only 全自动闭环
  真 Codex CLI 端到端跑通（CP7.3 冒烟 ce11d00）。M6 建造剩：CP7.4 §7.3 机制验收（mock 驱动真组件）+
  CP7.5 长跑收尾（不依赖下述缺口，可续建）。

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**M0–M5 全完成；M6 进行中**：CP7.1（da38b2f 自终止安全网）+ CP7.2（2ce750e StageProvider 真 Codex
适配器）+ CP7.3（ce11d00 run.py 全系统装配入口——**真 Codex CLI 冒烟通过**：一条命令跑通 bootstrap 全自动
闭环）已完成并记账。测试基线 **524 绿**。**系统「完整运行、进入全自动」核心已达成**（reasoning-only
真 Codex 端到端）。M6 refsheet 在 scratchpad/M6_refsheet.md。**建造/执行边界**（ROADMAP 已裁决）：
§7.4 T1/T2 真跑属运维。

## ⚠ 设计发现（2026-07-08）· plan 制品契约二分 → real-Codex attack 阻塞（已面陈用户，ROADMAP 详载）
勘查接 attack 到真 Codex 时发现：冻结 `plan.schema` target（抽象：target_key/spec_md/claim/eval_action）
与 attack_stages/harness 消费的执行 TARGET_SPEC（具体：train_cmd/smoke_cmd/eval_cmd/canonical_key/…）
**仅 {eval_key,seq} 重叠**（13 执行字段 schema 不含）→ 经 StageProvider（schema 校验）接 attack，Codex
产抽象 plan 拿不出 harness 跑训练的命令。**设计级缺口**（需裁 plan 契约：解冻 schema 携命令 / 加翻译层 /
TARGET_SPEC 作独立执行制品），触及冻结 schema（MIGRATION_SHA256 锁），超检查点级——**已面陈用户待其定向**。
**影响面**：仅真 Codex attack 轮（=§7.4 运维前置）；reasoning-only 全自动（已真跑）+ §7.3 机制验收
（mock 驱动真组件）**不受影响**。

## CP7.4/7.5 目标（不依赖上述缺口，可续建；等用户对 plan 契约的定向后再接 real-Codex attack）
- **CP7.4 §7.3 机制验收剧本集成测试**：mock provider 驱动**真组件**端到端串联断言（验状态机+不变量，
  非真 Codex）——主链路 baseline→多变体→多 evaluation→对照下结论（I1/I2/I3）/import 三失败路径（license
  deny/smoke 失败/eval 失败）/日志 suspect→复现/人机安全四负例。多数组件已有单测，本步做**§7.3 命名
  场景的端到端串联**（范式仿 test_m4_semantic_cases）。
- **CP7.5 M6 步级验证收尾**：长跑漂移断言（mock 数百轮 + kill-9 一致 + τ 自停）+ §7.3 全过 + §7.1 M6 勾兑。
- **运维就绪（§7.4 前置，须先裁 plan 契约）**：judge provider + idea/plan 消费者↔schema 校准 + sidecar 桥。

## 步⑦（M6）下一步动作
1. 等用户对 plan 契约缺口的定向（若要接 real-Codex attack）。
2. 无论定向如何，CP7.4 §7.3 机制验收 + CP7.5 长跑收尾均可先建（不依赖缺口）——仿 test_m4_semantic_cases
   建 test_m6_mechanism_scenarios（§7.3 四场景命名断言，复用 test_attack_advance/test_import_worker 设置）。
3. §5 循环：内审 Opus → codex ≤2 轮 → commit → build_log 0032。

## CP7.4 硬前置（务必先解，接 attack 全链前）
- ①attack_stages._idea_stage 读 c["content_md"]/audit_score 与冻结 idea_set.schema（core_claim/mechanism/
  audit_mapping/novelty_*+wildidea_extra）不符——过 schema 的候选会 KeyError；接 idea 到真 Codex 前校准
  attack_stages↔schema（改消费者读法，schema 冻结不动）。
- ②stage-emitted resource_request sidecar → notify.create_file_request 桥（CP7.2 暂 fail-loud 占位）。
- ③judge provider（真 Codex 评审 verdict + 写 runner_call(audit)+DECISION(judge)；范式见
  test_attack_advance.py:59 的 mock judge）。

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
