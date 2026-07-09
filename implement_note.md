# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步⑧（M7）CP8.5**（sidecar→file_request 桥）
- 检查点状态：构建+自验完成（617 测绿，+4）：interfaces.StageBlockedOnResources + StageProvider 桥
  （bridge 注入；桥拒→重试反馈；未接桥 fail-loud 保持）+ JudgeProvider 拒 sidecar + advancer 捕获
  （在途轮保持游标）+ run.py 装配 FileRequestService/bridge + 全等待环 E2E（阻断→precheck 拦→resolve→
  同一在途轮续跑）。内审（Opus）进行中 → codex 外审 → 提交 → build_log 0038。

## 正在做什么
**步⑧：plan 契约缺口补齐 → 全流程 real-Codex attack + 正式直接可用**（用户 2026-07-09 两道指令）。
已落 CP8.1–8.4（613 测绿）：**真 Codex 完整 attack build 链端到端跑通**（冒烟第二次：真 Codex 注册协议
@1[自声明 2 metric]、占坑并训练 MLP baseline、真子进程 acc=0.9949、双评审 pass、legal 入池、τ score_floor
真实触发；第一次冒烟暴露 kind 教学缺口+拒因不回流 → 已修成自纠环）。
剩余=正式可用收口三件（用户升格必做）：CP8.5 桥 / CP8.6 plan 全形态受理 / CP8.7 运维文档+步级收口。

## 下一步动作（按序）—— CP8.5 sidecar→file_request 桥
1. StageProvider._produce 的 resource_request.json 处理：fail-loud 占位 → 真桥：
   - 校验 sidecar 过 resource_request.schema（validator("resource_request")）；
   - 经注入的 file_request 服务（notify.FileRequestService.create_checked）落请求单（幂等：同内容不重复建）；
   - 抛专用异常（如 StageBlockedOnResources，携 request id）——advancer/attack_stages 把它转「轮干净收尾 +
     全局等待」（precheck 已会因 pending 文件请求阻断下一轮：notify.make_advancer_precheck 既有）。
   ⚠ 设计点：阶段半途发请求 → 本轮怎么收？最省事且诚实：attack 轮=该阶段业务失败收尾（inconclusive）+
     请求单已建，解除后重攻；reasoning-only 轮同理。核 notify.create_checked 签名与 interaction_request
     语义（CP6.3 落的：全局等待+提醒，无自动超时）。
2. run.py 装配桥（FileRequestService 需要 daemon+policy+work 根）；测试：sidecar → 请求单建 + 轮收尾 +
   precheck 阻断 + 用户 resolve 后续跑（notify 侧已有 resolve 测试可借力）。
3. 内审(Opus) → codex 外审(≤2轮) → 提交 → build_log 0038。
4. 接 CP8.6：exec/eval target kinds（gate_claim_variant / eval_action append·create 驱动；manifest 的
   eval-kind 分支 schema 已备）+ route 特化（reuse_only/eval_only/dependency_wait——derive_next_route 矩阵
   已在，缺 PlanOutcome 真来源）+ import_defer→DeferredImporter（orchestrator/importer.py 既有）+
   ImportWorker 装配 run.py。
5. 接 CP8.7：README 运维操作面（启动命令/goal_brief 写法/policy 旋钮/console·status_card·文件请求交互/
   恢复与停机语义/代理 7890）+ 步⑧步级验证三条全跑留证 + ROADMAP/本文件终态。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；外审 diff 排除记账类。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- CP8.5 关键事实：
  - StageProvider._produce 现对 resource_request.json 直接 raise RunnerError（fail-loud 占位，
    stage_provider.py ~L100）；sidecar 校验 schema 名 "resource_request"（不在 ARTIFACT_SCHEMA_MAP，
    经 validator() 直取——schemas.py 注释有说明）。
  - notify.FileRequestService.create_checked（CP6.3）：schema→幂等→enabled→items 上限→quota→create；
    make_advancer_precheck 已把 pending 文件请求作为全局阻断源。
  - 真 Codex 冒烟工作区：scratchpad/smoke_m7（第一次）、smoke_m7b（第二次，build 链全通证据库）。
- 测试基线 **613**。真 Codex 冒烟需代理 7890。
