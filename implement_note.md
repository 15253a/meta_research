# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步⑧（M7）CP8.6**（exec target kind）
- 检查点状态：内审 + codex 第1轮各 REQUEST_CHANGES 全修（内审 1 BLOCKER exec kill-9 楔死；codex 2 BLOCKER
  [append 放宽过头同 variant 错格污染 / exec 自占未核 config 漂移] + 2 SHOULD[orphan 显式清理 / 补测] +
  NIT）→ 623 测绿 → **codex 外审第2轮进行中**（bhtzmq2r7）。build_log 0039 待起。
  **模型裁量收窄 CP8.6=exec 一种**（eval frozen schema 无 variant 引用需设计、import 独立子系统→CP8.6b）。

## 正在做什么
**步⑧：plan 契约缺口补齐 → 全流程 real-Codex attack + 正式直接可用**。已落 CP8.1–8.5（618 测绿）：
真 Codex 完整 attack build 链端到端跑通（冒烟 acc=0.9949 legal 入池）+ 文件请求全等待环闭合。
剩余：CP8.6（本检查点）+ CP8.7（运维文档+步级验证收口）。

## 下一步动作（按序）—— CP8.6 plan 全形态受理（正式可用性②）
1. **exec target kind**（既有 legal baseline 上建变体）：
   - _derive_plan：exec 目标校验 claim{baseline_ref,variant_key,config_json}；baseline_ref 解析
     （canonical_key 字符串→baseline id，须 legal）；variant_key 未占核。
   - claim 段：gate_claim_variant（返回 variant_id+build_target_id！注意它**自己建 build_target**——
     与 build 的终局统一事务模式不同，须理顺：让 exec 走 gate_claim_variant 建的 bt，终局事务只补
     plan_ref/eval_key/required？或改为 exec 也统一终局建（gate_claim_variant 的 bt 创建将重复）——
     **设计点，开工时先读 gate_pool.gate_claim_variant 再定**。
   - bundle 驱动：manifest target_kind=exec（schema 已备 build/exec 同约束）；register 走
     gate_register_variant（非 register_baseline）。
2. **eval target kind**（免训练评估）：manifest 只须 eval 命令（schema 已备）；无 run/checkpoint；
   eval_action=create_evaluation（gate_register_evaluation create）或 append_attempt（append 模式 +
   gate_start/finish_attempt？核 §4.2.5 eval 目标的 attempt 语义——附录/既有 gate_exec 有
   gate_start_attempt/gate_finish_attempt）。{ckpt} 占位符来源=被评对象 checkpoint（claim/eval 定位）。
3. **route 特化**：_setup_cycle 现对 attack intent 恒 route='attack'——按 plan 后 outcome 特化需要
   plan 先行……但 route 在 setup 定、plan 在轮内跑。§2.3 规则5「route 在 plan 后特化」——现实现是
   起手 attack、plan 落什么算什么。特化真正需要的：dependency_wait（import_defer/claim 撞占用→写
   question_dep+释放 Qn+机械收尾不经 reasoning）。eval_only/reuse_only 可作为 attack 轮内自然形态
   （targets 全 eval / 空）先不改 route 字面——**开工时按 §4.2.5 原文裁量，注意别过度工程**。
4. **import_defer 接线**：_plan_stage 的显式拒 → 改为真受理：DeferredImporter（orchestrator/importer.py，
   CP4.3 select_deferred 幂等守卫）落 external_import+占位 baseline+question_dep(pending) → 轮收
   dependency_wait；ImportWorker 装配进 run.py（advancer.import_worker= + judge 需 import 侧 subject
   装配器——CP8.3 注记：JudgeProvider 是 attack 专用，import 布局不同！给 ImportWorker 配一个
   import-subject 版 judge 或复用 test 的写库范式？**开工先读 import_worker.resume_cycle/材料需求**）。
5. 测试：exec 全链（先 build 得 legal baseline 再 exec 变体）/ eval 目标（append+create 两模式）/
   import_defer→dependency_wait→物化→resolve dep / route 矩阵回归。真 Codex 冒烟可选（多轮成本高，
   mock E2E 为主 + 保留既有冒烟证据）。
6. 内审(Opus) → codex 外审(≤2轮) → 提交 → build_log 0039。
7. 接 CP8.7：README 运维操作面 + 步⑧步级验证三条全跑留证 + 终态记账。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；外审 diff 排除记账类。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- CP8.6 关键事实：
  - gate_claim_variant(baseline_id, variant_key, config_json, cycle_id, seq, question_id)→
    {variant_id, build_target_id}——**它自己建 exec build_target**（与 build 终局统一建不同）。
  - gate_register_variant(variant_id, build_target_id, evaluation_id, cycle_id, subject_hash, run_id)。
  - gate_exec 有 gate_start_attempt/gate_finish_attempt（失败 attempt 入账用；成功 attempt 只在
    gate_register_evaluation 单事务里生）。
  - manifest schema：eval kind 只须 commands.eval（禁 train/smoke）；exec 同 build（smoke+train+eval+
    checkpoint+repro_cmd_md）。
  - attack_stages 现状：_derive_plan 对 target_kind != build 一律 _PlanReject；_bundle_stage 命令序列
    按 build 静态；manifest._check_manifest 拒非 build。
  - derive_next_route 矩阵已在（advancer.py）：attack+blocked/import_deferred→dependency_wait、
    仅 eval→eval_only、空→reuse_only——PlanOutcome 从未被真填过（PlanOutcome() 默认全 False）。
  - importer.py=DeferredImporter（plan 侧三写入）；import_worker.py=物化 worker（resume_cycle 已接
    advancer.import_worker 探测）。
- 测试基线 **618**。真 Codex 冒烟需代理 7890；冒烟证据库 scratchpad/smoke_m7b。
