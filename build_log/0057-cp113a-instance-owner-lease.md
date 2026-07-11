# 0057 · CP11.3a 单实例 owner lease 与生命周期 fencing

- date: 2026-07-11
- commit: d79d3d024d8576a7baccba6950b2aaacd5bab94f — feat: 闭合单实例 owner 边界
- branch: fix/architecture-hardening-20260709
- 检查点 / 步: CP11.3a（属：步⑪生产硬化 · CP11.3 状态与执行边界）

## 决策

本检查点把“单 writer”从单 Python 进程内约定提升为共享 work-root 的进程级能力。生产 `build_system()` 在
打开 DB、启动 listener 或调用 Runner 前，先取得稳定 `.orchestrator-instance.lock` 的非阻塞 `flock`；
owner metadata 只用于诊断，是否允许 takeover 只由内核锁决定。原子 heartbeat 发布 owner generation、PID、
状态、序号和 freshness deadline；console 只有在 flock、metadata generation、heartbeat identity/freshness 和
`state=running` 同时成立时才显示 running，否则把 DB 在途状态诚实投影为 interrupted。

owner guard 覆盖 WriteDaemon 事务/COMMIT、Runner、attack manifest spawn、入站 spool ACK/cursor/retry、出站
send/receipt 和 worker 启停。`System.close()` 先封住新 public operation，再停 listener/pump/delivery，终态化已
接纳 query，关闭只读/写 DB，最后发布 stopped heartbeat 并释放 lock FD。构造失败与 close 失败均保留可重试
cleanup capability；heartbeat 启动后的异步异常会先 stop/join writer，descriptor release 由非 daemon worker 按
state→work→lock 顺序完成，并以 tombstone 后 Event 为完成权威，主线程 `KeyboardInterrupt` 不能遗失仍持锁 OFD。
fork child 在 at-fork handler 中只 close 继承 FD，不 `LOCK_UN` 父 owner。

同时把 console/connector durable cursor 改成 batch generation CAS：复核 current/target 双 anchor、严格递增
ordinal，允许同批逐条提交与已验证幂等，拒绝旧 consumer 回退新游标。status reader 使用共享 probe flock，多个
observer 不再互相伪装成真实 owner；claimant 的排他锁仍是唯一权威。

## 改动文件

- `meta-research/README.md` — 修改：记录单实例启动、embedding close 契约、heartbeat 观测与 CP11.3b 边界。
- `meta-research/orchestrator/__init__.py` — 修改：登记 instance lease 模块。
- `meta-research/orchestrator/instance_lease.py` — 新增：稳定 flock、owner generation、原子 heartbeat、fork/异步异常安全 acquire/close、只读状态复核。
- `meta-research/orchestrator/run.py` — 修改：lease-first 装配、owner-guarded Runner、System operation/run/worker/DB 的 lease-last 可重试关闭。
- `meta-research/orchestrator/writedaemon.py` — 修改：查询/事务入口及 COMMIT 前 owner fence，失败回滚。
- `meta-research/orchestrator/attack_stages.py` — 修改：stage 边界和 smoke/train/eval spawn 前 owner fence。
- `meta-research/orchestrator/connector_ingress.py` — 修改：owner guard 绑定、重绑 generation reset、ACK/cursor/retry/listener fence。
- `meta-research/orchestrator/connectors.py` — 修改：出站 send/receipt fence、scheduler 与底层 HTTP transport 机械 liveness。
- `meta-research/orchestrator/console_spool.py` — 修改：durable cursor generation/anchor/ordinal CAS，拒绝 stale rewind/ABA。
- `meta-research/orchestrator/console_server.py` — 修改：instance owner 活性投影及 interrupted 模式。
- `meta-research/orchestrator/status_card.py` — 修改：明确 response heartbeat 与 instance heartbeat 分离。
- `meta-research/orchestrator/interfaces.py` — 修改：补充 owner-guard/liveness 接口地图。
- `meta-research/views/console/index.html` — 修改：前端 owner 心跳与 interrupted 诚实显示。
- `meta-research/tests/console_smoke.js` — 修改：覆盖 interrupted/alive 真载荷与 demo 兼容。
- `meta-research/tests/test_instance_lease.py` — 新增：真实进程竞争、SIGKILL/fork/takeover、路径身份、schema、heartbeat、异步异常与 lifecycle 反例。
- `meta-research/tests/test_connector_ingress.py` — 修改：入站 owner guard、重绑 rescan 与 foreign work-root 反例。
- `meta-research/tests/test_connectors.py` — 修改：出站 guard 和真实 transport liveness 回归。
- `meta-research/tests/test_console_server.py` — 修改：active/interrupted 投影与 cursor CAS 回归。
- `meta-research/tests/test_run.py` — 修改：同 work-root 顺序重启显式 close，适配默认 lease 契约。

## Review

- 内部多路复核覆盖 instance lease/fork/OFD、System lifecycle、connector transport、console cursor/活性；先后修复
  generation 发布顺序、close/run/active-operation 竞态、accepted query 与 transport 残留能力、rebind cache、
  status false-active、fork 发布窗口和异步异常回滚。两轮外审后的最终内部复核结论：无 BLOCKER；另有手工构造
  `System` 时 accepted-only callback 默认值/显式 owner guard 两项泛化 SHOULD，生产 `build_system()` 路径已有
  accepted-only mediator callback 和 DB/Runner 内层 guard，留后续收紧。
- 首选模式 A `codexro-review` 因 codexro refresh token 失效（HTTP 401）未产出 verdict；内置 provider 的模式 B
  WebSocket 多次重连无 verdict，后改用同账号/同 gpt-5.5/xhigh 的 SSE 只读 provider 完成有效评审。
- 有效第 1 轮：`REQUEST_CHANGES`。采纳 stopped heartbeat 写失败仍释放 flock、join 裸异常、cursor O(n)/重复
  offset、probe 注释与 README 语义；“shutdown 禁止 private cleanup”经生产 callback 控制流与新增测试核实为
  误报，`close()` 本来就用 shutdown-only `mediator.poll` 重试有限 accepted 集合。
- 有效第 2 轮：`REQUEST_CHANGES`，达到两轮上限。反馈指出 descriptor release 被异步异常打断可能遗失 flock FD，
  以及 heartbeat thread start 后构造回滚未先 join。按 §2.2 不再送第 3 轮，改为 release worker+Event 和
  start-aware rollback；后续内部复核又补 direct cleanup handle 与共享 observer probe。最终复核实测真实 SIGINT
  后可立即 replacement acquire，未发现死锁、双 writer、FD reuse 或 at-fork OFD 泄漏。

## 验证

- 开发期按用户要求只跑相关验证：lease/run `75 passed`；connector `84 passed`；console `64 passed`；
  attack/WriteDaemon/interfaces `60 passed`；最终异步异常与 status 修复后 `tests/test_instance_lease.py` 为
  `38 passed in 1.99s`（提交前测试断言修正后再次为 `38 passed in 2.02s`）。
- 实际 VEPFS 同机进程竞争：
  `test_real_process_same_root_is_busy_but_different_root_can_run --basetemp=.pytest-vepfs-cp113a` →
  `1 passed in 0.24s`。这不能代替跨节点/目标挂载参数的两节点验收。
- `python -m py_compile`（本检查点 Python 模块）、`git diff --check`、`git diff --cached --check` 均通过。
- 按用户要求仅运行一次全量：`python -m pytest -q meta-research/tests`（仓库根）→
  **`1 failed, 1180 passed in 239.93s`**。唯一失败是新增测试错误地断言整个 pytest 进程不存在任何同名 heartbeat
  thread；全量上下文里其他旧测试保留不同 work-root 的 daemon observer，故为测试隔离假阳性。改为只跟踪该
  用例实际启动的 thread 后，按用户“不重复全量”要求仅定向复跑：`tests/test_instance_lease.py` →
  **`38 passed in 2.02s`**。没有伪报“全量全绿”，也没有运行第二次全量。
- 结论：功能路径和失败用例相关验证通过，检查点提交；全量证据保留上述单一测试假阳性及定向修复说明。

## 遗留 / 回退

- CP11.3b 尚须把普通 Codex、manifest/import/harness 执行统一纳入 process-group/cgroup supervisor，并闭合 timeout
  terminal receipt。当前 owner 被 SIGKILL 后 flock 可接管，但旧的未受管子孙仍可能与新 owner 重叠写 staging，
  因此本提交不能单独称为完整 crash-safe production recovery。
- CP11.3 后续仍须闭合 plan target `critical/budget_estimate` 权威落库/早退、严格 goal lineage/current goal、
  runner heartbeat/timeout receipt；CP11.4 与真实 100+ 轮验收也未完成。
- 若生产可能跨节点共享 VEPFS，必须在实际两节点与挂载参数上验证同时 acquire 只有一方成功；单机实测不足以
  证明分布式锁部署语义。
- 回退：先停止 orchestrator/listener/worker 并确认无在途外部调用，备份 work-root 的 DB、state 与 connector
  sidecar，再执行 `git revert d79d3d0`。本提交无 DDL migration；稳定 lock/heartbeat 文件可保留，旧版本会忽略，
  但 Git revert 不会自动终止已运行的新版本进程或删除耐久 sidecar。
