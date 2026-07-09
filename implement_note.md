# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步⑧（M7）CP8.7**（运维操作面文档 + 步级验证收口）
- 检查点状态：待开工（CP8.6 已提交 a713509 + 记账中）

## 正在做什么
**步⑧：plan 契约缺口补齐 → 全流程 real-Codex attack + 正式直接可用**。已落 CP8.1–8.6（623 测绿）：
真 Codex 完整 attack **build + exec** 链端到端跑通（冒烟 build 链 acc=0.9949 legal 入池）+ 文件请求全
等待环闭合。剩余：**CP8.7（本检查点，「直接可用」最后一块）** + CP8.6b（eval/import/route，独立拆出）。

## 下一步动作（按序）—— CP8.7 正式可用收口（正式可用性③）
1. **README 运维操作手册**（meta-research/README.md 扩写或新 docs/OPERATING.md——决策性制品，走评审）：
   - 启动：`python -m orchestrator.run --system-root . --work-root <dir> [--max-cycles N]`（需代理 7890
     供真 Codex；工程配置环境变量 METARESEARCH_CODEX_BIN/MODEL/EFFORT/RUNNER_TIMEOUT_S 见 runner.py）。
   - goal_brief 写法：input/goal_brief.md 的 YAML frontmatter（predicate_json 成功谓词，缺/非法即启动
     失败）+ 中文正文；toy 现例已在。
   - policy 旋钮：policies/policy.yaml 全量注册表（budget/tree_guard/flow.tau 等；改动=决策性）。
   - 人机交互：console（directive/query 入站）、status_card（work/state/status_card.json 观测面）、
     文件请求（阶段发 resource_request→请求单→resolve 到 input/user_provided/）。
   - 恢复/停机：同 work_root 重启即续跑（DB 权威、kill-9 无半写）；停因 = τ 自终止/pause 阻断/
     文件请求等待/max_cycles。
   - 系统边界（诚实）：canary——staging 净土物化+哈希对账，真 git worktree 隔离/env lock 强校验属后续
     硬化；eval/import target kind 与 route dependency_wait 特化 = CP8.6b。
2. **步⑧步级验证三条留证**（跑 + 贴 build_log）：①全链 E2E（test_full_attack_flow_end_to_end 过）；
   ②真 Codex CLI 冒烟（重跑一次 build+可选 exec，贴库态）；③frozen 锁（test_frozen_contracts + 623 全绿）。
3. **可选补跑真 Codex exec 冒烟**（成本高，非必须——exec 已 mock E2E + kill-9 恢复证；若跑，goal_brief
   引导「先 build 家族再做变体对照」）。
4. README 是决策性制品：内审(Opus) → codex 外审(≤2轮) → 提交 → build_log 0040 + 步⑧收尾（步级验证行
   写全）+ ROADMAP/本文件终态（步⑧「直接可用」达成声明 + CP8.6b 遗留）。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；外审 diff 排除记账类。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- 用户两道指令（2026-07-09）：①补齐缺口能完整走完整个流程；②最后要正式直接可用系统。CP8.7 是②的收口。
- 现有 README：meta-research/README.md（看现状，可能是早期骨架）。冒烟证据库：scratchpad/smoke_m7b（build 链）。
- 真 Codex 完整闭环命令已验：`python -m orchestrator.run --system-root . --work-root <tmp> --max-cycles 6`
  （第二次冒烟 exit 0：真 Codex 注册协议/占坑/训练 MLP/acc=0.9949/双评审/legal 入池/τ 触发）。
- CP8.6b（步⑧后续，非 8.7）：eval target（frozen schema create_evaluation 缺 variant 引用需设计）+
  import_defer→DeferredImporter（importer.py 既有）+ ImportWorker（import_worker.py 既有，需装配 run.py +
  import 版 judge subject 装配器[JudgeProvider 是 attack 专用]）+ route dependency_wait（derive_next_route
  矩阵已在，PlanOutcome 从未真填）。
- 测试基线 **623**。真 Codex 冒烟需代理 7890。
