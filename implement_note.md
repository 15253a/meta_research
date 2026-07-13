# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-13 ｜ 位置：步⑪ CP11.4c.3d.2 目标生产运行 / 最终验收
- 检查点状态：空闲；CP11.4c.3d.1 功能 `423dd78` 已提交，记账完成

## 上一检查点结果

- 已解除一个真实生产装配矛盾：deployment preflight 要求主 service non-root，但旧 tool-free
  query/adapter Runner 又只允许 root guardian。现在 non-root service 以当前 UID 在空 0700 临时
  cwd 执行；root 开发仍必须降到独立 `codexro`/显式非 root UID。
- 两路仍关闭 host/web/plugin/multi-agent 工具并严核 JSON trace、输出 owner/type/link/size；worker
  环境只含固定运行字段和代理/TLS 白名单，跨 UID 明确使用 `env -i`。responder 冻结
  policy/UID/bin/model/effort，Runner 不声明或发生漂移都会在执行前 fail-closed。
- Runner/Query **71 passed**、外审边界 **8 passed**、装配修正 **3 passed**、adapter/preflight
  **41 passed**；内部终审 APPROVE。外审第 1 轮 401；第 2 轮 REQUEST_CHANGES 的两个 BLOCKER
  与一个 SHOULD 均已修复，依两轮上限未发第 3 轮。依用户要求未跑仓库全量。

## 当前可用边界

- 默认 production 装配的 query/import adapter sideband 现在与 non-root preflight 契约兼容，不再是
  “preflight 能启动、首次 query 必失败”。修复没有增加第二 DB、daemon、scheduler、SSH
  orchestration 或通用 workflow。
- CP11.4c.3c 薄工具已闭合：qualification firewall、shared SQLite boundary、local/two-node
  canary 协议、fixed-linear fault runner 与 evidence pack 可组合使用。
- evidence v1 证明包内 bytes 闭包和一轮续跑，不是 restore engine，不声称完整 work-root DR、
  来源签名、真 Codex/GPU/two-node 或 qualification 已验收。
- 当前节点仍只有单机且无 NVIDIA container runtime；目标 GPFS 两节点正向、真实 ≥200 轮、
  T1/T2、故障注入组合验收和最终全量均未执行。

## 下一步动作

1. 进入 CP11.4c.3d.2：先解决当前节点 Docker `/ebs` ENOSPC，并在 dedicated VM/private cgroup +
   NVIDIA Docker + 目标 GPFS quota/second node/connector 到位后，按既有 runbook 执行真实 ≥200 轮。
2. 把 two-node canary、预声明 fault schedule、干净 restore+续跑 evidence pack 与 T1/T2 qualification
   作为同一生产验收批次，保存 manifest hash 到外部不可变审计记录。
3. 中间只跑相关验证；最终验收提交前只跑一次仓库全量，不反复制造长验证成本。

## 关键坑

- `reference/` 原始是单机 embedded SQLite WAL；两节点 VEPFS 是后续生产加固，不得倒称原始要求。
- evidence 目录 hash 是内容身份而非来源签名；没有外部保存 hash 时，同 UID 重写整包不能靠自证发现。
- `real_codex_resume_verified` / `qualification_receipts_verified` / `full_restore_verified` 仍必须为 false；
  不得用注入式 worker 的一轮续跑替代生产验收。
- 当前 host 能看到 8×A100-80GB，但 Docker 没有 NVIDIA runtime/cgroup/resource limit，且 `/ebs`
  backing store 已满；full-attack E2E 在 container create 阶段因此 ENOSPC，不得归因于本次 Runner 代码。
- root overlay 仍接近/处于满额；pytest basetemp 必须放 VEPFS 并及时清理。
