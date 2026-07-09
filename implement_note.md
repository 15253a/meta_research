# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步⑧（M7）CP8.2**（attack_stages 真契约化）
- 检查点状态：内审全修（586）→ codex 第1轮 REQUEST_CHANGES（3 BLOCKER[复用协议 metric 绑定/target_key·seq·
  metric_id 唯一/plan artifact 获取在 try 外]+2 SHOULD[resume 未 re-validate manifest/bundle 契约违规楔死]全修，
  593 测绿）→ **外审第 2 轮进行中**（codex 后台 b182slwam；out=scratchpad cp82_r2_out.md）。已 git add。
  build_log 0035 草稿备好（scratchpad build_log_0035_draft.md，待填第2轮结论）。

## 正在做什么
**步⑧：plan 契约缺口补齐 → 全流程 real-Codex attack**（用户 2026-07-09：补齐缺口，成品要能完整走完整个
流程）。方案：不解冻冻结件；execution_manifest 作 bundle 编译产物承载执行契约；attack_stages 走正式 gate
通道。全案见 ROADMAP 步⑧节。
- **CP8.1 已完成**（commit 8b4f59a + 记账，578 测绿）：execution_manifest.schema + orchestrator/manifest.py
  （校验/交叉核/净土物化正反对账/禁 shell·路径·env·超时围栏/checkpoint_dest/run_manifest_command）+
  policy.execution 节。契约层就绪，供 CP8.2 消费。

## 下一步动作（按序，具体到命令/文件）—— CP8.2 施工图定稿在 scratchpad **cp82_design.md**（先读它）
1. attack_stages.py 重写三阶段（施工图逐条）：
   - _idea_stage：content_md 机械合成（core_claim/mechanism/assumptions/MFE），audit_score/status 校准冻结
     idea_set.schema（现读 c["content_md"] 会 KeyError——schema 无此键）。
   - _plan_stage：走 gate_new_protocol（I1，name→int id 复用/分配）+ gate_claim_baseline（I5）；机械派生
     protocol_id/metric int 映射/eval_key/target_set_hash；resolved slice（冻结 target + 绑定四件）存 plan_ref；
     persist-then-consume（plan.json 先原子落 work/c<ci>/plan.json）；业务拒路径落 decision+空 targets 不楔死。
   - _bundle_stage：每目标唯一 staging（src=t<bt>/src 净土物化）；providers['bundle'] 产信封 →
     MF.validate_manifest+cross_check(slice)+stage_bundle_files；命令按 kind 静态序列 smoke→train→eval；
     MF.run_manifest_command 驱动；checkpoint 用 MF.checkpoint_dest；register 用 slice 的绑定四件 +
     manifest env_hash/identity.md/repro_cmd_md。
2. AttackStages ctor 加 schemas 参；providers 契约注释重写（删 TARGET_SPEC 段，+bundle）。
3. 冻结件回归测试：sha256(plan.schema.json) + database.MIGRATION_SHA256 断言不变。
4. test_attack_advance.py fixtures 全量改（schema-conform plan + bundle 信封 provider；崩溃恢复测保留）。
5. `cd meta-research && python -m pytest tests/ -q` 自验全绿 → 内审(Opus) → codex 外审(≤2轮) → 提交 → build_log 0035。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；外审 diff 排除记账类（build_log/、implement_note.md）。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- CP8.2 关键代码事实（已勘明，cp82_design.md 详载）：
  - manifest.py API：validate_manifest(schemas,m) / cross_check(m,slice) / stage_bundle_files(files,m,dest)→ledger /
    staged_hashes(dest)→ledger|None / checkpoint_dest(m,train_run_dir)→Path / run_manifest_command(m,kind,
    staging_dir,log_name,src_dir,work_root,policy,ckpt_path=None) / canon_hash(obj)。
  - resolved slice 契约（存 plan_ref，两侧共依赖）：冻结 plan.schema target 原样 + {protocol_id,protocol_ver,
    eval_key,target_set_hash}；plan_slice_hash=canon_hash(slice)。
  - DDL：protocol PK(id,ver)；metric_def PK(id,ver) int（plan.schema metric_id 是 string，须机械映射）；
    idea.content_md NOT NULL；build_target UNIQUE(cycle_id,seq)。
  - gate_new_protocol((id,ver) 已存在即拒→调用方须预检复用)；gate_claim_baseline(I5)；
    gate_register_baseline(identity_doc+repro_cmd)；gate_finish_build_target 支持 skipped(critical 早退)。
  - compiler render(stage='bundle',target_id) 锚区现为占位「完整计划切片=CP-M3」——CP8.2 补真切片渲染
    （build_target 行+协议+required_metric），否则真 Codex 看不到目标细节。
  - import_worker 走 external_import 行、plan_ref 存自己的 spec；与新契约按 target_kind 隔离，不动。
  - judge 写库范式：tests/test_attack_advance.py:59。
- 测试基线 **578**。真 Codex 冒烟需代理 7890。
