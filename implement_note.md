# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-13 ｜ 位置：步⑪ CP11.4c.3d.2 目标生产运行 / 最终验收
- 检查点状态：空闲；CP11.4c.3d.1.1 功能 `6aec828` 已提交，记账完成

## 上一检查点结果

- 真实 smoke 发现 d.1 的白名单 PATH 使用 Python `os.defpath=/bin:/usr/bin`，使默认
  `/usr/local/bin/codex` 的 `#!/usr/bin/env node` 误用 `/usr/bin/node` 12，Codex 0.144.0 启动即
  `SyntaxError`。现固定为 `/usr/local/bin:/usr/bin:/bin`，仍不继承用户 PATH。
- exact PATH 已进入 responder/Runner runtime contract，tool policy 升 v6；root 跨 UID 仍是
  `sudo ... env -i`，non-root same UID 仍只得到显式 safe env，没有扩大模型工具能力。
- 真实 bootstrap reasoning 完成 c1（success、16100 tokens）；从其已验证 snapshot 恢复干净诊断 target
  后，修复后的默认 query（未覆盖 binary/home）返回 `codex/success` 并记账 **9347 tokens**。
- Runner/Query **71 passed**、exact **3 passed**；内部审查 APPROVE。外审第 1 次因 wrapper 参数重复
  在启动前退出、无 verdict；第 2 次完整审查 APPROVE，未发现 BLOCKER/SHOULD/NIT。未跑仓库全量。

## 当前可用边界

- 默认 production 装配的 query/import adapter sideband 现在与 non-root preflight 契约兼容，且默认
  `/usr/local/bin/codex` 已有真实启动/应答证据，不再是“preflight 能启动、首次 query 必失败”。
- 单节点 development 的真实 Codex bootstrap reasoning + snapshot-grounded query 已达到基本可用；
  修复没有增加第二 DB、daemon、scheduler、SSH orchestration 或通用 workflow。
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
- root overlay 清理本轮 `/tmp/pytest-of-root` 后仅约 9MB 可用，仍处危险水位；`codexro` 本地 auth 已从
  root 主认证副本恢复为 owner 0600，诊断期间的 VEPFS 凭据副本已删除。pytest basetemp 必须放 VEPFS。
