# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-13 ｜ 位置：步⑪ CP11.4c.3d 目标生产运行 / 最终验收
- 检查点状态：空闲；CP11.4c.3c.3 功能 `6c05666` 已提交，记账完成

## 上一检查点结果

- 已增加 owner-only 非压缩 canonical evidence pack：`manifest.json + READY.json +
  objects/sha256/<digest>`，离线 verifier 不回开 source 绝对路径、不联网、不调 Docker。
- pack 从冻结 SQLite 重算 asset inventory、log mirror、repository root/target 与 dependency
  capability；repository 发布 ledger 以同一 `spec_ledger` 投影与 DB 对账，dependency 只打包
  receipt-bound 恢复语义文件，不把 builder 诊断日志/动态 sandbox metadata 冒充闭包。
- 恢复证明不根据 exit code 推断：`restore.json` 精确绑 source，target 从 adoption baseline
  **恰好新增一轮** done+route cycle，且有同轮 success research runner_call 与 ledger。
- evidence **26 passed**，import/dependency/repository related **22 passed**；内部终审 APPROVE。外审
  第 1 轮 401，第 2 轮 REQUEST_CHANGES 的成立项已在两轮上限后全修；未发第 3 轮。
  依用户要求未跑仓库全量。

## 当前可用边界

- CP11.4c.3c 薄工具已闭合：qualification firewall、shared SQLite boundary、local/two-node
  canary 协议、fixed-linear fault runner 与 evidence pack 可组合使用，没有增加第二 DB、
  daemon、scheduler、SSH orchestration 或通用 workflow。
- evidence v1 证明包内 bytes 闭包和一轮续跑，不是 restore engine，不声称完整 work-root DR、
  来源签名、真 Codex/GPU/two-node 或 qualification 已验收。
- 当前节点仍只有单机且无 NVIDIA container runtime；目标 GPFS 两节点正向、真实 ≥200 轮、
  T1/T2、故障注入组合验收和最终全量均未执行。

## 下一步动作

1. 进入 CP11.4c.3d：在 dedicated VM/private cgroup + NVIDIA Docker + 目标 GPFS quota/second
   node/connector 到位后，按既有 runbook 执行真实 ≥200 轮。
2. 把 two-node canary、预声明 fault schedule、干净 restore+续跑 evidence pack 与 T1/T2 qualification
   作为同一生产验收批次，保存 manifest hash 到外部不可变审计记录。
3. 中间只跑相关验证；最终验收提交前只跑一次仓库全量，不反复制造长验证成本。

## 关键坑

- `reference/` 原始是单机 embedded SQLite WAL；两节点 VEPFS 是后续生产加固，不得倒称原始要求。
- evidence 目录 hash 是内容身份而非来源签名；没有外部保存 hash 时，同 UID 重写整包不能靠自证发现。
- `real_codex_resume_verified` / `qualification_receipts_verified` / `full_restore_verified` 仍必须为 false；
  不得用注入式 worker 的一轮续跑替代生产验收。
- root overlay 仍接近/处于满额；pytest basetemp 必须放 VEPFS 并及时清理。
