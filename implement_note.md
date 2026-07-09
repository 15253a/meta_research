# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步⑧（M7）CP8.3**（生产装配：bundle SKILL + judge + StageProvider 扩展）
- 检查点状态：待开工（CP8.2 已提交 d822add + 记账中）

## 正在做什么
**步⑧：plan 契约缺口补齐 → 全流程 real-Codex attack**。已落：
- CP8.1（8b4f59a）execution_manifest 契约 + manifest.py 执法层。
- CP8.2（d822add）attack_stages 真契约化：idea/plan 消费冻结 schema；plan 走 gate_new_protocol/
  gate_claim_baseline；plan_ref=resolved 切片；bundle 逐目标 manifest 驱动真执行；全自动不楔死（业务拒）
  与损毁 fail-loud 分流。**mock provider 驱动真组件的完整 attack 轮已端到端跑通**（594 测绿）。

## 下一步动作（按序）—— CP8.3 生产装配（真 Codex 能产这些制品）
1. `prompts/skills/bundle/SKILL.md` 真执行契约改写（现为 M0 造假桩说明）：指示 Codex 产
   execution_manifest.json（照抄 pack 的 plan_slice_hash/protocol 绑定/required int）+ identity.md +
   代码文件；metric_value 行契约；argv/{src}/{ckpt} 占位符用法。**prompt=行为=决策性，走完整评审**。
2. judge：新 `schemas/review_verdict.schema.json`（verdict/issues/notes）+ `prompts/skills/judge/SKILL.md`
   + JudgeProvider（stage_provider.py 或新模块）：装 subject 材料（code review=切片+物化代码+smoke log；
   result review=metrics+eval log+identity）→ 真 Codex 调用 → 校验 verdict → 写 runner_call(phase='audit',
   status='success') + decision(actor='judge', payload{build_target_id,review_kind,round_no,verdict,
   subject_hash,runner_call_id,policy_hash})——范式 tests/test_attack_advance.py:59（daemon 由 provider 持有）。
3. StageProvider 扩展：_STAGE_FILES 加 bundle（required=[execution_manifest.json, identity.md]，**passthrough
   全部代码文件**——现 _produce 会丢未声明键，须为 bundle 放开）+ plan/idea 的 _CALL_NOTE 更新（TARGET_SPEC
   字样清理）。
4. 测试 + 内审(Opus) + codex 外审(≤2轮) + 提交 + build_log 0036。
5. 接 CP8.4：run.py 装配 attack 全家（AttackStages(schemas,policy) + judge/bundle provider）+ 全链 E2E +
   真 Codex CLI 冒烟 ≥1 完整 attack 轮（步级验证核心）。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；外审 diff 排除记账类。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- CP8.3 关键事实：
  - bundle pack 锚区已含（compiler_sqlite._bundle_target）：resolved 切片全文 + **plan_slice_hash**（manifest
    照抄）+ required int 绑定（eval 打印用）——SKILL 写法直接引用锚区字段名。
  - manifest 契约细节见 schemas/execution_manifest.schema.json description + orchestrator/manifest.py 模块注释
    （argv 禁 shell 启动器、{ckpt} 仅 eval、code_files 相对路径禁 . / ..、env 白名单、identity.md 必附复现节）。
  - runner_call.phase CHECK 含 'audit'；decision 无 schema 约束但 review_passed 判据面见 gate_exec.review_passed
    （verdict=pass + subject_hash 匹配 + runner_call success/audit/purpose=review_kind）。
  - CodexRunner.run_task(system_prompt, skill, context_pack)→Artifact(stage=pack.stage, files, md)；judge 材料
    是 staging 文件+DB 行——JudgeProvider 自装 prompt（不经 compiler；ContextPack 需 cycle_id/stage 字段，可造
    轻量 pack 或独立 runner 入口，看 runner.py _build_prompt 依赖哪些 pack 字段）。
  - StageProvider._produce 现只返回 spec 声明键（bundle 须 passthrough）；art.stage 漂移校验对 bundle 仍适用。
- 测试基线 **594**。真 Codex 冒烟需代理 7890。
