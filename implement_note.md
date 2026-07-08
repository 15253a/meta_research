# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 ｜ 位置：**M0–M6 建造面全完成**（空闲；剩余=运维执行 + 一处待用户裁的设计缺口）
- 检查点状态：空闲。系统「完整运行、进入全自动」**已达成**——reasoning-only 全自动闭环真 Codex CLI
  端到端跑通。测试基线 **535 绿**。

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**M6 建造面收尾**（步⑦ CP7.1–7.5 全落）：
- CP7.1 da38b2f 长跑自终止安全网（§4.4.6 τ：分数衰退 + ledger 预算，durable global_stop）
- CP7.2 2ce750e StageProvider（真 CodexRunner→(cyc,pack)→files 阶段回调，schema 校验+重试）
- CP7.3 ce11d00 run.py 全系统装配入口（**真 Codex CLI 冒烟跑通** bootstrap 全自动闭环）
- CP7.4 6be7566 §7.3 机制验收剧本（主链路 I1/I2/I3 + import 三失败 + 日志 suspect fail-closed + 人机负例）
- CP7.5 f45dd6f M6 长跑步级验证（守卫内不漂移 + 中途重启终库一致 + τ 自停）

**里程碑达成**：系统能一条命令（`python -m orchestrator.run --system-root . --work-root <dir>`）装配真
组件 + 真 Codex，全自动跑 reasoning-only 元循环到停机（provider terminate / τ 自终止），kill-9 可恢复、
人机可查询/指令、自终止安全网齐。**M0–M6 建造面全绿（535 测）**。

## 剩余（非本轮建造——运维执行 + 一处待用户定向）
1. **运维执行交付**（本 session 外，运维发起，真算力多日）：§7.4 T1/T2 真跑（真 Codex + 真 EEG 数百轮×
   24h，数据根 `/vepfs-mlp2/c20250511/250806010/mxm/paper1/data/EEG_Data`）+ 双模式 A/B 实测定默认。
2. **⚠ 待用户裁的设计缺口 · plan 制品契约二分 → real-Codex attack**（ROADMAP 步⑦「设计发现」+ commit
   06c4a70 详载）：冻结 plan.schema target（抽象 target_key/spec_md/claim）vs attack_stages/harness 执行
   TARGET_SPEC（具体 train_cmd/canonical_key…）仅 {eval_key,seq} 重叠 → 经 StageProvider（schema 校验）
   接 attack 到真 Codex，Codex 产抽象 plan 拿不出跑训练的命令。**需用户定 plan 契约方向**（解冻 schema 携
   命令[动 MIGRATION_SHA256]／加抽象→执行翻译层／TARGET_SPEC 作独立执行制品）。定向后接 real-Codex
   attack：judge provider（真 Codex 评审+写 runner_call/DECISION，范式见 test_attack_advance.py:59）+
   idea/plan 消费者↔schema 校准（attack_stages._idea_stage 读 c["content_md"] vs schema core_claim/
   mechanism…）+ stage-sidecar→notify.create_file_request 桥（StageProvider 当前 fail-loud 占位）。
   **影响面**：仅真 Codex attack 轮；reasoning-only 全自动 + §7.3 机制（mock 驱动真组件）不受影响。

## 已交付组件全景（M0–M6）
- 存储/守卫：database（冻结 DDL 三重锁）/writedaemon（单写短事务）/statestore_sqlite/gate_sqlite/
  gate_exec/gate_pool/phase_commit。
- 证据/观测：harness/obs_parser（suspect 真派生）/subject_manifest。
- 循环：advancer（run_cycles+恢复+status 发布+precheck 全局等待+**stop_controller 自终止**）/attack_stages/
  import_worker/**stopcontroller（M6 τ 安全网）**。
- 检索/编译：compiler_sqlite/recall_sqlite/budgeting/status_card（含原子发布器）。
- 人机（M5）：interaction/console/mediator/notify。
- **装配（M6）：stage_provider（真 Codex 适配器）/run.py（全系统入口）**。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体；**但 plan 制品契约缺口是唯一已上交
  用户定向的设计裁决**（触及 MIGRATION_SHA256 冻结锁，不自行解冻）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；APPROVE 才 commit；外审 diff 排除记账类。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- 真 Codex 全系统冒烟：`python -m orchestrator.run --system-root . --work-root <tmp> --max-cycles 1`
  （需代理 7890；reasoning-only 闭环）。attack 轮遇 NotImplementedError（main 干净报 exit 2，需 CP7.4 之后）。
- 测试基线 **535**；M6 各套：test_stopcontroller(13)/test_stage_provider(13)/test_run(9)/
  test_m6_mechanism_scenarios(8)/test_m6_longrun(3)。
- DDL/schema 冻结：改 schema 须同步 MIGRATION_SHA256（走评审）；policy.yaml 改动=决策性。
