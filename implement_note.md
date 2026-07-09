# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步⑧（M7）CP8.1**（execution_manifest 契约 + harness manifest 适配层）
- 检查点状态：内审 APPROVE + 外审第 1 轮 REQUEST_CHANGES（2 BLOCKER+2 SHOULD 全修，572 测绿）→
  **外审第 2 轮进行中**（codex 后台 bu8g09u5s；out=scratchpad cp81_r2_out.md）。已 `git add -A`。
  build_log 0034 草稿已备（scratchpad build_log_0034_draft.md，待填第2轮结论）。CP8.2 施工图定稿：
  scratchpad **cp82_design.md**（compiler bundle 锚区占位须补真切片、gate skipped 可用、import_worker
  plan_ref 按 kind 隔离不动 三勘察结论）。
- 外审第1轮修复（已 staged）：①_check_no_shell(argv[0]∈shell∪env 拒) ②checkpoint schema 负向前瞻拒 ..
  +checkpoint_dest 解析入口 ③staged_hashes symlink 审计+反向对账 ④哨兵损坏→ManifestError ⑤code_files
  uniqueItems ⑥env overlay 确认+加测。

## 正在做什么
**步⑧：plan 契约缺口补齐 → 全流程 real-Codex attack**（用户 2026-07-09：开始补齐缺口，成品要能完整走完
整个流程）。方案已与用户 + codex-chatgpt 对齐（不解冻冻结件；execution_manifest 作 bundle 编译产物；
attack_stages 走正式 gate 通道；canary 不接真 worktree）——全案见 ROADMAP 步⑧节 + scratchpad
`plan_contract_design_out.md`。
当前 CP8.1：新增 `schemas/execution_manifest.schema.json` + `orchestrator/manifest.py`（校验/交叉核/
占位符/围栏/staging 物化）+ policy.execution 节。纯新增，不改既有行为。

## 工作区状态
- ROADMAP.md 已登记步⑧（方案 + 验证方法 + CP8.1–8.6 切分）——未提交，随 CP8.1 检查点提交入库。
- 其余工作区干净（0033 已收尾，基线 535 测绿）。

## 下一步动作（按序，具体到命令/文件）
1. 等内审结论 → 修 BLOCKER/SHOULD。
2. codex 外审（≤2 轮）：`git add -A` 后导出 staged diff（排除 build_log/、implement_note.md），模式 B 内联后台跑。
3. APPROVE → 检查点提交（CP8.1）→ build_log 0034 + INDEX + 勾 ROADMAP + 刷本文件 → 记账提交。
4. 接着开工 CP8.2（attack_stages 真契约化，切分见 ROADMAP 步⑧）。

CP8.1 已产文件：schemas/execution_manifest.schema.json、orchestrator/manifest.py、tests/test_manifest.py（20 测）、
tests/fixtures/{valid,invalid}/execution_manifest/*、policy.yaml+policy.schema.json（execution 节）、
orchestrator/schemas.py（ARTIFACT_SCHEMA_MAP 注册）、两清单锁测试更新。559 测绿。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；APPROVE 才 commit；外审 diff 排除记账类（build_log/、implement_note.md）。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- 步⑧关键代码事实（已勘明，直接用）：
  - attack_stages.py:172 `_target_spec` 从 build_target.plan_ref 读 toy TARGET_SPEC（CP8.2 清理对象）；
    消费字段：smoke_cmd/train_cmd/eval_cmd/**ckpt_name**（非 ckpt_path）/env_hash/identity_draft_md/
    repro_cmd/protocol_id/protocol_ver/eval_key/target_set_hash/required。
  - gate_pool.gate_claim_baseline（I5 占坑）/gate_new_protocol（I1，(id,version) 重复即拒——replay 须调用方
    预检「已存在且内容同→跳过」）；gate_register_baseline 接收 identity_doc+repro_cmd（bundle 终版身份）。
  - DDL：protocol PK(id,version)（id 由编排器按 name 查复用/分配 max+1）；metric_def PK(id,version) int
    ——plan.schema 的 metric_id 是 **string**，须机械映射到 int（同 name/direction/unit/compute_spec 复用，
    否则分配新 id）；build_target UNIQUE(cycle_id,seq)；idea.content_md NOT NULL（schema 无 content_md，
    须由 core_claim/mechanism/assumptions/MFE 机械合成）。
  - compiler_sqlite.render 已支持 stage="bundle"+target_id（bundle pack 渲染就绪）。
  - judge 写库范式：tests/test_attack_advance.py:59（runner_call(phase='audit',status='success') +
    decision(actor='judge',type=review_kind,payload{build_target_id,review_kind,round_no,verdict,
    subject_hash,runner_call_id,policy_hash})）。
  - run_staged cwd=staging_dir、同名 final 拒（log_name 须唯一）；eval 侧车 .exit 先于 final 落。
- 测试基线 **535**。真 Codex 冒烟需代理 7890。
