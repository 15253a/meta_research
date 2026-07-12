# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-12 ｜ 位置：步⑪ CP11.4c.3b.2 存储运维闭环
- 检查点状态：CP11.4c.3b.1 已提交；转入 b.2 最小 verify/restore/retention/capacity/GC

## 上一检查点结果

- 功能提交 `83ace54`：每个终态 cycle 同步发布 exact SQLite backup → 中文 views Git →
  CAS manifest → immutable pointer；genesis/pending/owner/Git 父链和 startup/reentry/worker/abort 边界均 fail-closed。
- 相关 **105 passed**；内部代码和参考/文档审查 APPROVE。外审首轮 Git orphan 误读已用新增回归证伪，
  最终轮 CLI 挂起 5 分钟无 verdict。根盘只余约 155MB，低于已知全量 191MB basetemp，未启动必然 ENOSPC 的全量。

## 本检查点目标

不改 b.1 同步主干，只补薄运维闭环：离线 verify/restore、至少 3 代已验证 DB backup、由既有资源
envelope 派生的容量门、dry-run-first 有界 GC、raw-log 压缩镜像，以及 import-materialization
indexes/objects 可达闭包的盘点/恢复。registered checkpoint/content-store/log 原件不 GC。

## 完成性审计新发现

- 当前容器不满足 production：嵌套 Docker/kubepods、root、共享 0775 socket、cgroup driver none、无 NVIDIA runtime，
  Docker 总盘仅约 21GB；无第二节点与 GPFS hard quota 证明。不放宽 preflight。
- reference 要求的每轮 backup/snapshot 主干已由 b.1 实现；离线 restore/verify、retention、容量门和
  有界 GC 仍缺，是 b.2 生产 blocker。
- T1/T2 还缺 §7.4 sealed-holdout/label/trial/one-shot/non-feedback 输入域防火墙；两节点 canary、
  ≥200 轮 fault soak、evidence pack/restore verifier 尚未实现。
- CP11.3c 的 120 轮仍只是无真实 provider/训练的控制面回归，不作生产验收证据。

## 当前动作

1. 先固定 offline verifier/restore 的最小 CLI 契约，复用 b.1 manifest/backup/Git 链，不建第二套真相。
2. 在同一组操作里加 retention/capacity/GC；GC 先 dry-run，apply 只作用于已验证超额 backup 和无引用 staging。
3. 最后补 raw-log 压缩镜像和 import-materialization 可达闭包核验，全程保留 registered 原件。
