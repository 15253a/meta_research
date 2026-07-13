# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-13
- 位置：步⑪ CP11.4c.3d.2 目标设施真实运行与最终验收
- 最近功能检查点：`acb0684`（CP11.4c.3d.2a bounded resident exit + production/fixed_and_test handoff）
- 当前判断：生产代码、恢复/验收工具、目标运行协议与本机全量已闭合；目标设施和真实运行证据仍未闭合，
  CP11.4c.3d.2/CP11.4c.3d/CP11.4c.3/CP11.4c 均保持未完成

## 已闭合

- CP11.4c.3b 存储治理代码与运维工具已闭合：SQLite/views snapshot、离线 verify/restore、至少三代 backup、
  容量门与有界 GC、registered execution-log mirror、checkpoint raw CAS、import repository/dependency CAS，
  以及 SQLite → registered assets → import CAS 的 relocation-safe exact combined restore。
- CP11.4c.3c 验收工具已闭合：T1/T2 qualification firewall、两节点 shared-fs canary、固定 fault schedule runner、
  canonical evidence pack/offline verifier。production non-root tool-free Runner 及真实 plan/review/reasoning 恢复链已闭合。
- CP11.4c.3d.2a 已落 `acb0684`：`PRODUCTION_ACCEPTANCE.md` 串联最终 commit 冻结、production receipt、
  local/two-node canary、真实 connector、≥200 count gate、owner/payload/daemon/budget/resource faults、
  registered restore、T1/T2、building 机器矩阵与 `fixed_and_test`/用户裁决边界。
- `--exit-after-research` 保持 pause/file request 常驻，研究 terminate/max-cycles 后排空交互并 0 退出；默认常驻和
  `--once` 不变。owner-kill 用 137 专用 receipt + exact schedule verify；证据交接拒 symlink/hardlink/special file，
  per-run no-clobber 且 operator/shared 双 SHA manifest。
- 最新全量：`1843 passed, 1 skipped in 1634.46s`；`tests/test_run.py` 61 通过；compileall、bash fence
  `bash -n`、CLI help、staged diff check 均通过。两轮外审均 REQUEST_CHANGES，所有成立反馈在两轮上限后修毕。

## 当前环境实测

- 当前 host 为 root、GPFS work mount、宿主可见 8×A100；Docker socket 是 rootless proxy symlink，daemon
  `CgroupDriver=none`，无 memory/CPU/PID limits、无 NVIDIA runtime，Docker root 余量约 3.46 GiB。
- 没有权威 GPFS fileset byte+inode hard quota、production attestation、第二节点分配或生产 connector。
- 零轮正式 probe 写出 development receipt 且 `production_ready=false`；GPU/runtime/quota/UID/socket/cgroup/
  headroom checks 均按合同拒绝。
- GPFS single-node 五阶段 canary local/verify 通过，但诚实保持 `two_node_verified=false`、
  `shared_fs_ready=false`、`infrastructure_fence_verified=false`。这些不是生产通过证据。

## 仍未闭合

- 目标设施须提供两台不同 machine/boot 的 dedicated non-root VM、private cgroup + dedicated rootless Docker +
  NVIDIA runtime、同一 GPFS fileset/绝对路径、权威 byte+inode hard quota、独立归档故障域与 fence 责任人。
- 尚未执行真实 ≥200 轮（真实 Codex/import/materialization/train/eval/checkpoint/connector ACK），也未完成
  owner/payload SIGKILL、Docker daemon loss、budget exhaustion 与 resource/quota failure 的目标注入和恢复新成功轮。
- 尚未完成 two-node canary、STONITH/诚实 crash-stop 边界、registered combined restore、整 work-root/站点恢复演练、
  T1/T2 一次性 qualification、最终 clean commit 机器矩阵和用户真实运行签署。

## 下一步动作

1. 与用户按 `meta-researchv2/fixed_and_test/templates/真实运行验证记录.md` 在运行前冻结真实任务、观察目标、
   T1/T2 数据/label rule/seeds/folds/预算/主指标/统计检验/null-control 与成功/负结论口径。
2. 由目标设施 operator 按 `meta-research/PRODUCTION_ACCEPTANCE.md` 提供 canonical 路径、non-root service、fresh
   attestation、两节点/GPU/quota/connector 与独立 evidence archive；先跑零轮 production receipt 和 canary 门。
3. 执行真实 ≥200 轮与预声明 fault schedules，完成 connector 交互、registered mirror/restore、完整归档恢复和 T1/T2；
   未执行或 receipt 谓词不满足的矩阵行保持未验证。
4. 在最终 clean commit 跑唯一 building 出口全量，生成 per-run 机器矩阵/原始证据交给 fixed_and_test 审阅；
   用户依据运行前预期给出“符合/部分符合/不符合”及最终原话后，才可勾 CP11.4c.3/CP11.4c。

## 验收边界

- evidence-pack v1 只证明 manifest 声明的 SQLite/import-CAS exact-one-cycle resume 切面；
  `full_restore_verified=false` 时不能代签 registered hydration 或完整 work-root DR。
- 机器证据可以证明身份、哈希、一次性消费、恢复和结果；科学合理性、novelty、真实好用性与最终签署不能由模型代签。
- 在目标环境和用户冻结预期之前，不启动会消耗 sealed final/confirmatory 机会的 T1/T2 最终运行。
