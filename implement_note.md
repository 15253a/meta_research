# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-13 ｜ 位置：步⑪ CP11.4c.3d.2 目标生产环境 / 最终验收
- 检查点状态：代码级基本可用；CP11.4c.3d.1.4 功能 `43c0b38` 已提交，等待外部基础设施

## 上一检查点结果

- 全新三轮 smoke 在 c3 plan review 遇真实 Codex envelope `Extra data`；Runner 过去把 parse 失败统称
  `runner_error`，PlanReview 又对全部 RunnerError 直接 fail-loud，导致可修的格式错误炸穿整次入口。
- 现在无 fenced JSON、JSON decode 失败、顶层/files 形状非法统一标 `artifact_parse`；PlanReview 先完整记录
  本次失败成本/heartbeat，再仅对此类走 policy 的有界重试。transport/timeout/runtime 等仍不重试。
- 从同一 c3 持久 plan checkpoint 恢复后，plan review PASS，bundle、CPU Docker build/run、code/result review、
  reasoning 与 Gate 全完成；q2 answered，evidence 精确指向 `mr1/mr2`，storage verify 深验 c1-c3 全通过。
- 同一成功 c3 snapshot 的 console query 经真实 Codex rc11 success 并返回 grounded reply；相关 Runner/Provider
  **81 passed**、精确 7 passed。内部终审 APPROVE；依用户要求未跑仓库全量。

## 当前可用边界

- 默认 production 装配的 query/import adapter sideband 现在与 non-root preflight 契约兼容，且默认
  `/usr/local/bin/codex` 已有真实启动/应答证据，不再是“preflight 能启动、首次 query 必失败”。
- 单节点 development 已真实跑通 bootstrap、decompose、idea、plan/review、bundle、Docker execution 与双 review；
  **同一个新鲜 work-root** 又继续跑通 reasoning→Gate 关题、c1-c3 snapshot 深验和 tool-free query sideband。
  修复没有增加第二 DB、daemon、scheduler、SSH orchestration 或通用 workflow。
- CP11.4c.3c 薄工具已闭合：qualification firewall、shared SQLite boundary、local/two-node
  canary 协议、fixed-linear fault runner 与 evidence pack 可组合使用。
- evidence v1 证明包内 bytes 闭包和一轮续跑，不是 restore engine，不声称完整 work-root DR、
  来源签名、真 Codex/GPU/two-node 或 qualification 已验收。
- 当前节点仍只有单机且无 NVIDIA container runtime；目标 GPFS 两节点正向、真实 ≥200 轮、
  T1/T2、故障注入组合验收和最终全量均未执行。

## 下一步动作

1. 进入 CP11.4c.3d.2：在 dedicated VM/private cgroup + NVIDIA Docker + 目标 GPFS quota/second
   node/connector 到位后，按既有 runbook 执行真实 ≥200 轮。
2. 把 two-node canary、预声明 fault schedule、干净 restore+续跑 evidence pack 与 T1/T2 qualification
   作为同一生产验收批次，保存 manifest hash 到外部不可变审计记录。
3. 只在上述最终验收提交前跑一次仓库全量；当前不再重复长验证。

## 关键坑

- `reference/` 原始是单机 embedded SQLite WAL；两节点 VEPFS 是后续生产加固，不得倒称原始要求。
- evidence 目录 hash 是内容身份而非来源签名；没有外部保存 hash 时，同 UID 重写整包不能靠自证发现。
- `real_codex_resume_verified` / `qualification_receipts_verified` / `full_restore_verified` 仍必须为 false；
  不得用注入式 worker 的一轮续跑替代生产验收。
- 当前 host 能看到 8×A100-80GB，但 Docker 没有 NVIDIA runtime/cgroup/resource limit。清理本轮旧临时目录后
  本轮曾在 container create 遇 `/ebs` ENOSPC；清理临时 canary 后虽恢复并完成一个新鲜 CPU attack 轮，20G
  节点仍不具长跑 headroom。pytest basetemp 必须放工作根，生产长跑不得使用该节点。
- 当前新鲜 smoke 只有 3 轮/单节点/CPU/development/no-outbound；不能外推为 ≥200 轮、GPU、双节点、connector
  交付或 T1/T2 qualification 已通过。
