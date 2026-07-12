# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-12 ｜ 位置：步⑪ CP11.4c.3b 最小存储治理
- 检查点状态：CP11.4c.3a 已成对提交；开始勘查存储写入/发布/恢复边界

## 上一检查点结果

- 功能提交 `b2081a3`：生产只支持真实模式 A，schema/`System`/装配入口拒绝 B，构造后不可篡改；不新增
  第二套会话状态机。内部代码/文档审查与外审最终轮均 APPROVE，相关 **165 passed**。
- 检查点唯一全量为 1072 passed / 1 skipped / 5 failed / 421 errors；根盘开跑前仅余 175MB，运行中
  变为 0B，191MB basetemp 清除后恢复，错误主体为 pytest 无法创建临时目录。按约不重跑并如实留据。

## 本检查点目标

补足 reference 要求中真正阻塞生产长跑的最小存储治理：每轮 SQLite online 滚动备份、views 可审计 Git
时间线、不可变/CAS 资产 manifest/hash、原始日志分级归档、恢复演练、容量门与安全 GC。大 checkpoint 与
content store 只记录内容身份和保留引用，不做每轮全量复制。

## 完成性审计新发现

- 当前容器不满足 production：嵌套 Docker/kubepods、root、共享 0775 socket、cgroup driver none、无 NVIDIA runtime，
  Docker 总盘仅约 21GB；无第二节点与 GPFS hard quota 证明。不放宽 preflight。
- reference 要求的每轮 backup/snapshot、checkpoint 保留和 GC 尚未实现，属 CP11.4c.3b 生产 blocker。
- T1/T2 还缺 §7.4 sealed-holdout/label/trial/one-shot/non-feedback 输入域防火墙；两节点 canary、
  ≥200 轮 fault soak、evidence pack/restore verifier 尚未实现。
- CP11.3c 的 120 轮仍只是无真实 provider/训练的控制面回归，不作生产验收证据。

## 当前动作

1. 盘点现有阶段提交、status/views 发布、SQLite WAL/恢复、checkpoint/content/log 路径及已有保留策略。
2. 选一个薄的治理组件与一个明确调用边界；不引入后台服务、第二套数据库或通用工作流引擎。
3. 先用相关恢复/容量/GC 回归验证；检查点末才做一次全量并成对提交。
