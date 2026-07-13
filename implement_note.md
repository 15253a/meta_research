# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-13 ｜ 位置：步⑪ CP11.4c.3d.2 目标生产环境 / 最终验收
- 检查点状态：代码级基本可用；CP11.4c.3d.1.5 功能 `b208233` 已提交，等待外部基础设施

## 上一检查点结果

- 复核真实 c3 恢复证据时发现：Stage/PlanReview/Judge 过去以实例内 `_call_seq` 命名 transcript/heartbeat；
  checkpoint 重启后计数归零，新 `runner_call` 会复用路径，而 Runner 又会主动删除同名 out/events、覆写 prompt。
- 现在生产调用先持久取得 `runner_call_id`，再以 `rc<ID>` 命名 heartbeat 和 Codex prompt/output/events；
  created→running 与 heartbeat 路径绑定仍在同一 DB 事务完成，成本、execution/provider receipt 语义未变。
- 同步修复 query responder 原先硬编码的 `...-1.events.jsonl`，bound query 现在返回真实存在的
  `...-rc<ID>.events.jsonl`。两个全新 Provider/Runner 实例的旧、新证据均保留。
- cost/stage/runner/query/reconcile 相关 **179 passed**，内部终审 APPROVE；依用户要求未跑仓库全量。

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
- clean-node restore 仍是 `sqlite_truth_only`：历史宿主 transcript 不随 SQLite restore 复制；本检查点只保证
  原 work-root 内跨 checkpoint 不覆盖，不能把它外推成完整 transcript DR。
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
