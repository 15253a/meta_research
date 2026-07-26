# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-26 14:05 UTC ｜ 位置：空闲 / 下一检查点 CP11.4c.3d.2 目标环境真实验收
- 检查点状态：空闲

## 正在做什么

CP13.1–CP13.8 已完成、验证并提交。功能 commit 为 `1762f98`，施工记录见
`build_log/0092-cp13-bundle-target-dag.md`；当前没有剩余 CP13 代码动作。

## 工作区状态

- 分支 `fix/architecture-hardening-20260709`。
- 代码检查点纳入当前已验证的 107 文件 runtime 闭包；`.scratch`、execution 运行目录、
  `.superpowers`、参考资料和反馈图片未进入提交。
- 最终验证：全量 2765 passed、22 skipped；console/browser 97 passed；受影响 runtime 154 passed；
  prompt/contract 99 passed；compileall、diff check、非 root runtime assets 与独立内部终审均通过。
- 外审两轮的权限与 fixed Worker live-control BLOCKER 均已修复；closing TOCTOU/stale scope 后续加固
  也已复核，无剩余 blocker。

## 下一步动作（按序，具体到命令/文件）

1. 等待 dedicated VM/private cgroup+NVIDIA Docker、GPFS quota、second node/connector 等外部资源。
2. 资源就位后按 `meta-research/PRODUCTION_ACCEPTANCE.md` 执行真实 ≥200 轮、故障注入、T1/T2
   qualification 与用户签署，完成 CP11.4c.3d.2。

## 关键上下文 / 坑（新 session 不读会踩的）

- CP13 migration SHA256 为
  `5f2add9dcd5d6fbeb3c870fa677beccf175a472259fcadba2a48af50606b24aa`。
- `seq` 仅展示/tie-break；readiness 只由 dependency + exact admission 派生。
- journal live/recovery 共用 ordered frame identity；SQLite 不存 raw log。
- live repair/replan 必须由 scope 对应 Worker 消费；`control_accepting=false` 后请求未入队，须重读状态。
- 正常与 critical 终态都必须 drain；replay 缺任何 dependency/input/Worker/lease/review/terminal 证据
  继续 fail closed。
