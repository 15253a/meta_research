# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步⑧（M7）CP8.4**（run.py attack 全装配 + 全链 E2E + 真 Codex 冒烟）
- 检查点状态：待开工（CP8.3 已提交 c7863c0 + 记账中）

## 正在做什么
**步⑧：plan 契约缺口补齐 → 全流程 real-Codex attack**。已落三检查点（608 测绿）：
- CP8.1（8b4f59a）execution_manifest 契约 + manifest.py 执法层。
- CP8.2（d822add）attack_stages 真契约化（mock provider 全链 attack 端到端跑通）。
- CP8.3（c7863c0）生产装配：bundle/judge SKILL（m7-1）+ review_verdict schema + StageProvider bundle
  passthrough + JudgeProvider（attack 专用；写 runner_call(audit)+DECISION(judge)）。

## 下一步动作（按序）—— CP8.4 = 步⑧步级验证核心
1. run.py build_system 装配 attack 全家：
   - _STAGES += "bundle"；skills 加载 bundle + judge（prompts/skills/judge/SKILL.md 单独读，不进 _STAGES）。
   - PoolGate(daemon, open_gate_read_conn) + SqliteGate(close gate, parser_suspect=OP.suspect_for_attempt
     用独立读连) + AttackStages(state, compiler, pool_gate, close_gate, providers={idea,plan,bundle,
     reasoning=StageProvider 各回调, judge=JudgeProvider}, obs_policy=policy["observation"], work_root,
     schemas, policy) → SqliteAdvancer(attack=...)。装配范式照 tests/test_attack_advance._mk_env。
   - main() 的 NotImplementedError exit-2 分支改述（attack 已装配；剩余 NotImplementedError 源=exec/eval
     kind 的 bundle 侧、import 在途轮未装 worker——保留干净报错）。
   - **_plan_stage 补 import_defer 显式拒**（plan.schema 允许但 CP8.6 未接线：silently 当空 targets 会丢
     import 意图——转 _PlanReject 记录，防静默；小改随本检查点评审）。
2. E2E 测试（tests/test_run.py 扩）：runner_factory=脚本化 mock（按 stage 吐 schema-conform 信封，bundle
   吐真 toy 代码+manifest；judge 走真 JudgeProvider+mock runner 吐 review_verdict）→ build_system 跑
   bootstrap→(reasoning 选 attack)→attack 全链（真子进程 train/eval）→关问→terminate；断言池 legal/
   metric/answer/decision(judge) 全链。
3. **真 Codex CLI 冒烟**（步级验证②）：装 toy 冒烟 system-root（scratch：拷 prompts/schemas/policies +
   toy goal_brief.md「单一可直接实验的问题，无需分解」）→ `python -m orchestrator.run --system-root <toy>
   --work-root <tmp> --max-cycles 6`（代理 7890）→ 断言 ≥1 attack 轮完整走完（idea/plan/bundle 真执行/
   注册/关问）。留完整输出进 build_log。
4. 步级验证③既有：608 全绿 + frozen 锁在 CP8.2 落。
5. 内审(Opus) → codex 外审(≤2轮) → 提交 → build_log 0037 + 勾 ROADMAP 步⑧验证① ②。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；外审 diff 排除记账类。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- CP8.4 关键事实：
  - AttackStages ctor：(state, compiler, pool_gate, close_gate, providers, obs_policy, work_root,
    schemas=None, policy=None)——run.py 须传 schemas+policy（manifest 校验+围栏）。
  - 装配范式：tests/test_attack_advance.py::_mk_env（PoolGate/SqliteGate/open_gate_read_conn/OP.suspect_
    for_attempt 独立 obs 读连）。JudgeProvider ctor：(runner_factory, schemas, policy, system_prompt,
    skill, daemon, work_root)。
  - run.py 现状：attack=None + NotImplementedError exit 2；_STAGES=("idea","plan","reasoning")；
    compiler/publisher 用 open_responder_read_conn 只读连（单写纪律，别用可写连）。
  - StageProvider bundle 回调已有（sp.bundle）；真 Codex 冒烟先前范式见 build_log 0031（CP7.3）。
  - reasoning 轮选 attack 与否由真 Codex 决定——toy goal brief 要写成「单问题直接可实验」引导 attack。
- 测试基线 **608**。真 Codex 冒烟需代理 7890。
