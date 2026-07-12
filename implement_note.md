# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-12 ｜ 位置：步⑪ CP11.4c.3c.2b 目标 VEPFS 薄 canary / 预声明 fault runner
- 检查点状态：空闲；CP11.4c.3c.2a 功能 `75f8009` 已提交，记账完成

## 上一检查点结果

- 已修正 production GPFS/VEPFS 与 SQLite WAL 上游合同的根本冲突：已知本地文件系统用 WAL，
  GPFS/未知文件系统用 `DELETE` rollback + `synchronous=FULL`。
- 旧 WAL 在任何 schema/data 读前以 EXCLUSIVE 唯一 owner 模式迁移；SQL trace 锁定顺序，损坏的
  stale shm 不被接管进程使用。
- 共享盘 console 准入复用 lease boot identity；invalid/stale/remote 在 SQLite open 前拒绝并返回 503。
  它明确不是接管 fence，不消除外部 STONITH 要求。
- 相关验证：database **20 passed**，runtime **134 passed**，终审修正后 **65 passed**；
  内部双终审 APPROVE。外审两轮均因独立凭证 HTTP 401 无 verdict，已到上限。

## 当前可用边界

- 单机 reference 路径仍支持 WAL；当前 GPFS 路径不再依赖不受支持的跨 host WAL。
- 只支持旧节点已被基础设施 fence 后的 crash-stop 串行接管；网络分区但旧主存活仍不安全。
- 当前只看到一个节点，无 NVIDIA container runtime；两节点/正向 GPU/真实 ≥200 轮/最终全量均未完成。

## 下一步动作

1. 只做 CP11.4c.3c.2b：在现有 `InstanceLease`、guardian、artifact capability 和 storage ops 上组一个
   薄的 one-shot node/verify canary；不加常驻服务、第二 DB、scheduler 或通用 workflow engine。
2. fault schedule 用 canonical JSON 预先冻结 hash/事件 ID/触发点，只调用现有 run/storage 入口；每个事件
   恰执行一次，缺失/重复/冲突均 fail closed。
3. 当前环境无第二节点：先完成 CLI/schema/单节点负向与崩溃回归；不把模拟结果写成两节点通过。
4. 中间仍只跑相关验证；全量只在最终验收提交前跑一次。

## 关键坑

- `reference/` 原始是单机 embedded SQLite WAL；两节点 VEPFS 是后续生产加固，不得倒称原始要求。
- root overlay 仍接近/处于满额；pytest basetemp 必须继续放 VEPFS 并清理。
- 外审独立凭证当前 401；后续检查点仍每次最多两轮，不得因鉴权失败循环重试。
