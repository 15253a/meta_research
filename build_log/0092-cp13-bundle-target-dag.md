# 0092 · CP13 durable Bundle Target DAG runtime

- date: 2026-07-26
- functional commit: `1762f98af8a0d5aeb29f443170ca713333df6ebd` — `feat(orchestrator): land CP13 durable bundle DAG runtime`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP13.1–CP13.8（Bundle Target DAG、常驻 per-target Worker 与并发实验调度）

## 结论

Bundle 生产路径已从 cycle-wide 串行 target authoring 收敛为一个 cycle-wide Scheduler task 加每 target
一个固定、可恢复的 Worker task。耐久 DAG 和精确 admission 是唯一 readiness 真相，`seq` 只作显示与
稳定 tie-break；A 正式 admission 后 B/C 可在资源允许时并发执行，A admission 前下游保持零执行副作用。

执行日志使用文件侧 append-only ordered journal，首次/恢复读取有界 snapshot，常规监控使用 cursor
incremental read；默认 200、硬上限 1000，stdout/stderr、UTF-8 边界、半行、崩溃尾部和 owner-loss
恢复均绑定同一 durable frame identity。SQLite 只保留紧凑状态、cursor 与正式 ref/hash。

publication-backed source 只通过精确 manifest/hash binding 物化到 Worker 私有可写目录；GPU 由受信
runtime 从授权 contract 原子分配 exact durable lease，只有 guardian 证明进程树排空后才能释放。非关键
失败只跳过依赖后代，关键 replan fence 新调度并 drain；replay 缺 dependency/input/Worker/lease/review/
terminal 任一权威证据继续 fail closed。

## 决策与修改

- `bundle_graph.py` / migration `0002_bundle_target_dag.sql`：新增 target node/dependency/source request/
  binding/admission、resource request/lease、Worker/terminal report 与 Scheduler revision 的 additive
  durable schema；环、缺失、自依赖、跨 cycle、publication/legal/phase-commit 漂移均 fail closed。
- `bundle_scheduler.py` / `bundle_tasks.py`：Scheduler 只读紧凑图并派发 ready frontier；每 target 固定
  Worker provider identity，review child 每轮 fresh。正常完成与 critical failure 都必须显式 drain。
- `execution_journal.py` / `process_supervisor.py` / `harness.py`：guardian 以 ordered binary frame capture
  同时支持 live observer 与恢复；journal 用 disk sidecar index 做有界 pread，不在内存保存全历史。
- `bundle_sources.py` / `resource_leases.py` / `execution_sandbox.py`：正式 publication 源只读核验后私有复制，
  symlink/identity drift 拒绝；GPU lease 原子独占、资源不足只等待、cold restart reconcile 继续验证释放。
- `attack_stages.py` / `runtime_mcp.py` / `cycle_replay.py`：接入 concurrent Scheduler/Worker、精准失败传播、
  terminal report、recovery 与 replay。live repair/replan 按 scope 选择 per-target session，并由当前 Worker
  thread 精确消费；`control_accepting` 在 final snapshot 前关闭，消除 late control TOCTOU，stale scope
  不能误取消另一 target。
- 当前 HEAD 到工作树之间的 resident runtime/native review/scientific contract/runtime storage 依赖与
  CP13 调用链不可安全拆分；本功能提交纳入这份已由同一全量回归验证的 107 文件代码闭包。`.scratch`、
  execution 运行目录、`.superpowers`、参考资料和反馈图片均未进入提交。

## review

- 内部并行审查覆盖 durable lease/drain、normal completion contract、ordered stream identity、bounded
  journal/recovery、Scheduler revision、descendant skip replay 与 ABC 集成；发现的 cold-drain、normal
  drain、decoder/cursor、revision reset、lifecycle race 等问题均先补复现再修复。
- 外审第 1 轮：`REQUEST_CHANGES`。三个 BLOCKER 是 CP13 新模块/migration/Scheduler skill 的
  `0600/0700 root-only` 权限；统一改为文件 `0644`、目录 `0755`，并以无特权 `codexro` 用户验证
  import、migration 和 skill/plan 读取。
- 外审第 2（最终）轮：`REQUEST_CHANGES`。BLOCKER 是 fixed Worker 已使用 per-target session，但 live
  repair/replan 与 observer 仍绑定 cycle session。反馈成立，新增失败测试并改为 exact runtime session +
  current-worker owner。按两轮上限不再发第 3 轮。
- 第 2 轮后独立内部终审又识别 Worker final snapshot 后仍 `is_alive()` 的接受窗口和 stale legacy scope；
  以锁保护的 `control_accepting` closing barrier 与 active-target 校验闭合。最终复核确认无新 blocker、
  锁反转或 legacy cycle-wide 回归。

## 验证

- `pytest -q`：`2765 passed, 22 skipped in 366.70s`。
- `pytest -q tests/test_console_frontend.py tests/test_console_server.py`：`97 passed in 12.90s`。
- `pytest -q tests/test_attack_advance.py tests/test_runtime_mcp.py`：`154 passed in 78.21s`。
- closing/stale/fixed/legacy 精确复核：`14 passed`；Worker skill 同步后的 prompt/contract：`99 passed`。
- `python -m compileall -q orchestrator`、`git diff --cached --check` 均返回 0。
- 无特权用户成功 import 6 个 CP13 核心模块、读取 2 个 migration、Scheduler skill 与 plan。
- migration SHA256：
  `5f2add9dcd5d6fbeb3c870fa677beccf175a472259fcadba2a48af50606b24aa`。

## 遗留与回退

- CP13 当前无剩余代码动作。目标环境真实 ≥200 轮、dedicated VM/private cgroup+NVIDIA Docker、GPFS
  quota、second node、production connector 与 T1/T2 qualification 仍属于 CP11.4c.3d.2，未因本提交标绿。
- 功能回退：`git revert 1762f98af8a0d5aeb29f443170ca713333df6ebd`。migration 与审计数据是
  append-only 制品；回退代码不等于删除既有数据库事实、publication、journal 或 lease receipt。
