# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-12 ｜ 位置：步⑪ CP11.4c.3c.3 canonical evidence packer / offline verifier
- 检查点状态：空闲；CP11.4c.3c.2b.2 功能 `aa03a01` 已提交，记账完成

## 上一检查点结果

- 已新增同机前台 fixed-linear fault sidecar；v1 只接受全历史唯一 execution selector 上的
  `kill_owner` / `kill_execution_payload`，不启动/重启 owner，不含 DAG/plugin/shell/SSH/第二 DB。
- pidfd pin、spent-before-signal、owner/receipt 双重重扫、完整 authority 冻结、严格 hash chain 与
  action-specific aftermath 已闭合；spent gap 永不重杀，applied gap 只恢复观察，Ctrl-C 返回 130。
- README 已补 selector 预知/后知两种双终端 runbook、canonical schedule 示例、退出码与诚实边界。
- 定向 **16 passed**；fault/lease/guardian/qualification 相关回归 **86 passed**；内部双终审无
  BLOCKER/Major。外审两轮均因独立 reviewer 401 无 verdict，已到上限；全量仍留最终检查点。

## 当前可用边界

- CP11.4c.3c.2 的 SQLite boundary、五阶段 shared-fs canary 与预声明 fault runner 已闭合；目标 GPFS
  两节点正向仍须由不同 machine/boot 的操作者实跑。
- fault v1 只在真实外调 running receipt 上注入 owner/payload crash；cycle-boundary kill、随机故障、
  daemon restart 和恢复正确性不由该 sidecar 冒充。
- 当前只有一个节点且无 NVIDIA container runtime；STONITH、正向 GPU、真实 ≥200 轮、T1/T2 与最终
  全量均未完成。

## 下一步动作

1. 只做 CP11.4c.3c.3：canonical evidence packer + offline verifier，复用既有 snapshot/CAS/fault/
   qualification receipt，不加 DB、daemon 或通用归档工作流。
2. 在不存在的新 work-root 完成离线 restore 后，至少启动既有 `orchestrator.run` 续跑一轮并把结果纳入 pack；
   不能只验证文件 hash 就写 recovery 通过。
3. 中间继续只跑相关验证；全量只在最终验收提交前跑一次。

## 关键坑

- `reference/` 原始是单机 embedded SQLite WAL；两节点 VEPFS 是后续生产加固，不得倒称原始要求。
- fault receipt 的 `signal_exactly_once=false` / `recovery_verified=false` 是硬诚实边界；evidence packer
  只能在干净 restore + 真续跑后给恢复结论，不能改写既有 runner receipt。
- root overlay 仍接近/处于满额；pytest basetemp 必须放 VEPFS 并及时清理。
- 外审独立凭证当前 401；后续检查点仍每次最多两轮，不得因鉴权失败循环重试。
