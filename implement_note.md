# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.3c 状态语义收口
- 检查点状态：CP11.3b 已提交 `2f4a5d5`（build_log 0058）；CP11.3c 待开工

## 正在做什么

CP11.3b 已把默认外部执行的同机可信 descendant-tree 生命周期闭合：shared supervisor 的 guardian 持
delegated instance fence，以 owner pipe + PDEATHSIG 检测父死亡，以 subreaper/session + TERM→KILL + ECHILD
证明整树排空，terminal receipt/output fsync 后才释放 fence；System 在 DB/lease 前关闭 supervisor。普通/
tool-free Codex、manifest/harness 与显式 import smoke/eval 已统一接线。

下一检查点 CP11.3c 回到 reference 的业务状态面：把 plan target `critical/budget_estimate`、严格 current goal
lineage、runner/evaluation heartbeat，以及 timeout/owner_lost execution receipt 与 runner_call/run/attempt/target
的权威终态、重试、通知对账闭合。CP11.3b receipt 当前证明的是 OS 进程树事实，不能替代这些 DB 语义。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- 已提交：CP11.3b 功能 `2f4a5d5`；build_log 0058 与 ROADMAP/现场快照已入库。
- 唯一全量：`1 failed, 1209 passed in 269.66s`；唯一失败为 console backlog 的通用/专用阻断文案时序断言，
  隔离复跑通过，放宽到稳定 `入站待处理` 子串后定向 `1 passed, 44 deselected`；按用户要求未二跑全量。
- 外审已达两轮上限：第 1 轮凭据 401 无 verdict；第 2 轮 REQUEST_CHANGES，误报/采纳/不采纳见 build_log 0058。
- 当前仍是 operational canary，不是 reference-complete 或 hostile-workload production-ready 系统。

## 下一步动作

1. 对照 reference 审计 plan schema→build_target 的 `critical/budget_estimate` 数据路径与非关键失败早退语义。
2. 审计 goal_amend 后所有新执行/证据的 current goal lineage，以及 runner_call/run/evaluation_attempt 残留状态。
3. 设计 receipt→DB 对账和 heartbeat：owner_lost/timeout/cancelled 的权威映射、崩溃恢复、重试/通知；开发期仍
   只跑相关验证，检查点最后只跑一次全量。
4. CP11.3c 后再进入 CP11.4 敌对隔离/补账/内容寻址，并做真实 100+ 轮运维验收。

## 关键上下文 / 坑

- `reference/` 是设计权威，不是运行时自动加载的 skill；CP11.3b 只补齐 execution lifecycle 子概念。
- 当前 backend 不是 cgroup/container：要求 Linux `/proc`、prctl、signal 权限；PID namespace PID1 拒绝，
  tool-free 异 UID 回收要求 root。同 UID hostile workload、guardian/receipt 篡改留 CP11.4。
- 同机 GPFS/VEPFS owner-kill canary 已通过；跨节点 flock 仍须真实两节点同时 acquire 验收。
- `ImportWorker` 组件要求同 owner fenced supervisor，但默认 build_system 尚未装配 import resumer；fetch provider
  未来若执行 git/subprocess 也必须进入 supervisor。
- 不得 push；下一检查点仍须先内部审查、最多两轮 codex 外审、一次最终全量、功能提交 + build_log 提交。
