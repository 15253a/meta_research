# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步⑧（M7）CP8.8**（部署首跑发现的 reasoning selection 楔死 bug 修复）
- 检查点状态：构建+自验完成（625 测绿，+2）。用户要求把系统部署到 fixed_and_test_factory 并真跑验证——
  真 Codex 首跑 5 轮成功（3 baseline legal + 真测量 0.97–0.99）后 c6 reasoning 楔死（Codex 选 attack 已达
  visit 上限的题→persist_selection 未捕获 ValueError→持久化 reasoning 重启确定性重崩=永久楔死）。已修
  （persist_selection_safe: 非法 selection→decision+terminate 干净收尾；compiler 标注 attack 不可调度题；
  advancer 路径不改[非 persist、rollback 契约]）+ 2 回归。内审（Opus）+ 部署复跑（second_run，8 轮）并行中。
  **注：无 web 组件**（系统是 CLI orchestrator，用户「查看 web」实为看系统能否跑）。

## 已达成（用户 2026-07-09 两道指令收口）
① 补齐 plan 契约缺口、能完整走完整个流程；② 最后是正式直接可用系统。→ **步⑧（M7）CP8.1–8.7 全落**：
- CP8.1 `8b4f59a` execution_manifest 机器执行契约 + manifest.py 执法层（围栏/校验/物化）。
- CP8.2 `d822add` attack_stages 真契约化（消费冻结 plan.schema + 正式 gate + manifest 驱动真执行；全自动不楔死）。
- CP8.3 `c7863c0` bundle/judge SKILL（m7-1 真执行契约）+ review_verdict schema + StageProvider/JudgeProvider。
- CP8.4 `4f39559` run.py attack 全装配 + 全链 E2E + **真 Codex CLI 冒烟 build 链跑通**（acc=0.9949 legal 入池）。
- CP8.5 `650aace` 文件请求全等待环（阶段 sidecar→请求单→干净停→precheck 拦→resolve→同一在途轮续跑）。
- CP8.6 `a713509` exec target kind（既有 legal baseline 上建变体，register_variant 入池；崩溃自占复用严核）。
- CP8.7 `d627a39` 运维操作手册（README §0–§8）+ 步⑧步级验证三条收口留证。

**系统现状**：一条命令 `python -m orchestrator.run --system-root . --work-root <dir>` 让真 Codex 全自动跑
idea→plan→bundle→reasoning 元循环——build+exec target 真训练/评估/双评审/注册入池、真证据关问、decompose/
terminate/τ 自停、文件请求全等待、kill-9 崩溃恢复、人机 pause/resume/query、全自动不楔死。不解冻任何冻结件
（plan.schema/DDL/MIGRATION_SHA256 字节未变，frozen_contracts 锁证）。

## 剩余（非本轮建造；诚实边界，README §7 载）
1. **CP8.6b**（步⑧后续，独立检查点，非阻塞交付）：eval target（frozen schema 对 create_evaluation 无
   variant 引用，需设计）+ import_defer→DeferredImporter（importer.py 既有）+ ImportWorker 装配 run.py
   （+ import 版 judge subject 装配器——JudgeProvider 是 attack 专用）+ route dependency_wait 特化
   （derive_next_route 矩阵已在，PlanOutcome 从未真填）。遇到这些系统当前干净业务拒、不楔死。
2. **运维执行 / 硬化**（本 session 外）：①成本记账接线（`INSERT INTO ledger`——接后全局成本安全网
   budget.session_max 生效，当前休眠）；②真 git worktree 隔离 + env lock 强校验（canary→硬化）；
   ③§7.4 T1/T2 数百轮真跑（真 Codex + 真 EEG，数据根 /vepfs-mlp2/c20250511/250806010/mxm/paper1/data/
   EEG_Data）；④双模式 A/B 实测定默认。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；外审 diff 排除记账类。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- 真 Codex 全自动闭环命令（已验）：`python -m orchestrator.run --system-root . --work-root <tmp> --max-cycles 6`
  （需代理 7890）。冒烟证据库：scratchpad/smoke_m7b（build 链全通）。运维面见 meta-research/README.md。
- 步⑧关键组件：orchestrator/{manifest,attack_stages,stage_provider,run,advancer,gate_pool,compiler_sqlite,
  harness}.py；schemas/{execution_manifest,review_verdict}.schema.json；prompts/skills/{bundle,judge}/SKILL.md。
- 测试基线 **623**（步⑧新增 attack 全链 manifest 驱动 + exec + 恢复剧本 + 文件请求环 + frozen 锁 + judge 链）。
