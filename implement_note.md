# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-12 ｜ 位置：步⑪ CP11.4c.3c.1 T1/T2 输入防火墙（下一检查点）
- 检查点状态：空闲；b.2b.2 功能已提交 `d72da35`，正在完成记账

## 上一检查点结果

- 已从 repository/dependency-image runtime verifier 抽出纯文件 inspector；runtime current policy、
  allowlist 与 Docker resolve 仍在薄包装中，离线核不调网络/Docker/当前 policy。
- selected SQLite backup 通过 worker selection lineage 精确绑定 candidate index，再闭合 repository object
  与 v3 dependency-image；100 targets 去重核验，legacy/new plan shape 不可互相降级。
- 一步 restore 在 SQLite target 初次可见前即带 exact root marker，随后以 target flock 接管，不新造状态机；
  object/index/receipt 三个 crash window 均保持目标不可启动并可重放，复用条目重新 fsync，容量按目标 block/目录项预算。
- 相关验证：storage import **14 passed**、dependency inspector **5 passed**、repository inspector/
  runtime **5 passed**、ImportWorker 投影 **3 passed**、既有 SQLite restore **4 passed**；未跑全量。
- 内部三路最终 `APPROVE`；外审第 1 轮凭证 401、第 2 轮约 5 分钟/49k tokens 无 verdict，已到两轮上限。

## 当前可用边界

- 百轮级 snapshot 主干、last-3 深验/GC、已登记日志镜像及 import/dependency CAS 离线核验/恢复
  已具备检查点命令；均不每轮自动全量扫描。
- 当前仍不是完整 DR：SQLite 与 import CAS 分两条显式 restore；log 正本、checkpoint/content store、views Git
  不在恢复闭包；同一 VEPFS failure domain 不防 fileset/站点丢失。
- production preflight、T1/T2 firewall、两节点 canary、≥200 轮真实 fault soak 与 evidence pack 仍未完成。

## 下一步动作

1. 下一检查点只做 CP11.4c.3c.1 的 sealed-holdout/one-shot/non-feedback 机械输入防火墙；先读 reference §7.4，
   优先复用现有 artifact capability、sandbox 与 evaluation gate，不加第二套实验调度器。
2. 中间只跑 firewall/qualification 相关验证；全量仍只在最终检查点执行。
3. c.2 两节点 canary/soak runner、c.3 evidence packer 后再进入目标 ≥200 轮运行。
