# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-12 ｜ 位置：步⑪ CP11.4c.3c.2b.2 预声明 fault schedule runner
- 检查点状态：空闲；CP11.4c.3c.2b.1 功能 `fb65955` 已提交，记账完成

## 上一检查点结果

- 已增加一个固定五阶段、前台 one-shot shared-fs canary；没有新增 daemon、DB、scheduler、SSH 编排
  或通用 workflow engine。
- local/two-node scope 不可互相升级；exact owner SIGKILL/reap、wrapper 父死清理、post-kill guardian
  Busy、hot rollback、FD path replacement 和 cleanup terminal receipt 已机械闭合。
- GPFS 定向实跑证明 DB dirty/hash 与 hot-journal magic 后再 crash，恢复 hash 精确回 baseline；`/tmp`
  WAL local CLI 也 exit 0，但诚实保持 `two_node_verified=false`。
- 相关验证：canary **17 passed**，database/lease/guardian/FD **85 passed**；内部终审无 BLOCKER/Major。
  外审两轮均因独立凭证 HTTP 401 无 verdict，已到上限；全量留最终检查点。

## 当前可用边界

- 单机 reference 路径仍支持 WAL；GPFS 路径使用 `DELETE/FULL`，canary 可由操作者在目标两节点运行。
- 当前只完成 process-crash canary；网络分区但旧主存活仍不安全，基础设施 STONITH 仍是外部要求。
- 当前只看到一个节点，无 NVIDIA container runtime；两节点/正向 GPU/真实 ≥200 轮/最终全量均未完成。

## 下一步动作

1. 只做 CP11.4c.3c.2b.2：一个前台 one-shot fault schedule CLI，输入为预先冻结的线性 canonical JSON；
   不做 DAG、plugin、arbitrary shell、远程调度或常驻进程。
2. v1 只支持能绑定现有 durable authority 的 `kill_owner` / `kill_execution_payload`；spent 必须先于 signal，
   signal delivery 无法证明的 crash gap 写 inconclusive，不盲目重杀、不声称 signal exactly-once。
3. schedule/spent/applied/result/final 全部 no-clobber，触发点只读现有 execution receipt/cycle snapshot；
   status card/heartbeat 不作为触发权威。
4. 中间仍只跑相关验证；全量只在最终验收提交前跑一次。

## 关键坑

- `reference/` 原始是单机 embedded SQLite WAL；两节点 VEPFS 是后续生产加固，不得倒称原始要求。
- canary 代码外围 receipt 校验仍偏 verbose；本轮已将状态图压到五阶段。后续不再增加 phase，若精简须
  作为行为不变维护，不能与 fault 语义一起重写。
- root overlay 仍接近/处于满额；pytest basetemp 必须继续放 VEPFS 并清理。
- 外审独立凭证当前 401；后续检查点仍每次最多两轮，不得因鉴权失败循环重试。
