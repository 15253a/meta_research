# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-13
- 位置：步⑪ CP11.4c.3d.2 目标环境真实运行与用户验收
- 最近功能检查点：`866afda`（CP11.4c.3b.2b.3 registered checkpoint/log recovery）
- 当前判断：生产代码、存储恢复工具与本机回归已闭合；生产环境证明尚未闭合，不能宣称最终 production-ready

## 已闭合

- CP11.4c.3b 存储治理代码与运维工具已闭合：SQLite/views snapshot、离线 verify/restore、至少三代 backup、
  容量门与有界 GC、registered execution-log mirror、checkpoint raw CAS、import repository/dependency CAS，
  以及 SQLite → registered assets → import CAS 的 relocation-safe exact combined restore。
- append-only DB refs 不改写；恢复 receipt 保存全部 historical path roots，多跳恢复前拒绝 target 与任一 lineage
  root 相等或互嵌。registered continuation marker 只有在 exact source authority、逐文件 hydration、target receipt
  和 import CAS 全部复验通过后才解除。
- CP11.4c.3c 验收工具已闭合：T1/T2 qualification firewall、两节点 shared-fs canary、固定 fault schedule runner、
  canonical evidence pack/offline verifier。production non-root tool-free Runner 及真实 plan/review/reasoning 恢复链已闭合。
- 最新全量：`1841 passed, 1 skipped, 0 failed`；本检查点定向为 storage 63、evidence 30、相邻 198；
  `compileall`、storage CLI help、staged diff check 均通过。两轮外审最终 `APPROVE`。

## 仍未闭合

- 当前 host 不满足目标生产前提：缺 dedicated non-root VM/private cgroup + NVIDIA Docker runtime、权威 GPFS
  byte+inode quota、第二节点和生产 connector；因此尚无两节点正向、GPU 正向或真实生产运行证据。
- 尚未执行真实 ≥200 轮（含真实 Codex/import/训练）的长跑，也未按预声明 schedule 完成 owner-kill、daemon-loss、
  budget/resource failure 等故障注入。
- 尚未在目标环境完成 T1/T2 qualification、干净节点 restore/resume、完整证据归档及用户参与的最终验收。
- 当前 registered mirror 位于 source storage subtree；它解决逻辑损坏/误删和 relocation 恢复，不替代独立故障域的
  fileset/整 work-root/跨站灾备。runner/guardian/qualification/uploads/views/connector 等仍由各自 authority 归档。

## 下一步动作

1. 与用户按 `meta-researchv2/fixed_and_test` 冻结目标机器、真实运行预期、T1/T2 数据和验收签字边界。
2. 在 dedicated non-root VM/private cgroup + NVIDIA Docker、GPFS hard byte+inode quota、第二节点和生产 connector
   就位后运行 production preflight，并完成两节点 canary、GPU 正向与恢复演练。
3. 执行真实 ≥200 轮及固定故障日程，完成 T1/T2 qualification；保存 evidence pack、registered raw
   mirror/verify/restore 输出与 completion receipts，由用户参与最终验收后再勾 CP11.4c.3/CP11.4c。

## 验收边界

- evidence-pack v1 只证明 manifest 声明的 SQLite/import-CAS exact-one-cycle resume 切面；
  `full_restore_verified=false` 时不能代签 registered hydration 或完整 work-root DR。
- 机器证据可以证明身份、哈希、一次性消费、恢复和结果；科学合理性、预期结果与人工验收不能由模型代签。
- 在目标环境和用户冻结预期之前，不启动会消耗 sealed final/confirmatory 机会的 T1/T2 最终运行。
