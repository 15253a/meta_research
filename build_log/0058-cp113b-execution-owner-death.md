# 0058 · CP11.3b 执行 owner-death 边界

- date: 2026-07-11
- commit: `2f4a5d52270e083cd5843d75dce9da6954930a1b` — feat: 闭合 CP11.3b 执行 owner-death 边界
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.3b（属：步⑪ CP11.3 状态与执行边界）

## 决策

CP11.3a 的 instance flock 只能排斥两个 orchestrator owner；父进程被 `SIGKILL` 时无法运行 `finally`，旧的
Codex/manifest 子孙仍可能写 staging。故本检查点新增共享 `ExecutionSupervisor`，把默认外部执行交给独立
guardian，而不再让 orchestrator 父进程自己冒充生命周期权威：

- 每次执行先发布 private `prepared` receipt；guardian 继承同一 instance flock open-file description 的
  duplicate，设置 nonfatal `PR_SET_PDEATHSIG(SIGUSR1)` 并监听 owner-only pipe，兼顾 parent-death race 与
  pending-spawn fork 继承窗口。
- guardian 是 Linux child subreaper，payload 经过 durable running barrier 后在新 session exec；timeout、cancel、
  owner death、直接子进程退出但仍有后代均走 TERM→KILL，最终只认 `waitpid(-1, WNOHANG)` 的 `ECHILD` 为整树
  已空证明。输出 fsync、terminal receipt 原子发布后才释放 delegated flock。
- parent 对异常 guardian、receipt 身份/目录 inode 漂移、ambiguous `Popen` 结果永久 poison；不允许并发新 spawn，
  也不把 `close()` 伪报成功。`prepared` 只有在 replacement 已取得 instance fence 后才可恢复为
  `owner_lost_before_start`；旧 `running`/损坏 receipt 一律 fail-closed。
- 普通 Codex、不同 UID 的 tool-free query、manifest smoke/train/eval、harness 与显式 ImportWorker smoke/eval
  统一走 supervisor；System 关闭顺序加入 supervisor→DB→lease。tool-free 要求 root，避免 sudo 降权子树因
  signal 权限不足永远无法排空。
- harness 保持 `.partial → .exit → final` 提升纪律，并写 `<log>.process.json` 便利指针；中央 execution receipt
  才是权威。pointer 失败不覆盖原 timeout/owner_lost 分类，成功路径 pointer/exit 耐久失败则不提升 final。

本环境没有挂载/委派 cgroup，故 backend 明确为 `linux-subreaper-session-v1`：只承诺具有可见 `/proc`、
`prctl`/signal 权限的**可信同机 descendant tree**，不把它宣传为敌对代码沙箱。跨节点 VEPFS、同 UID 恶意
workload、guardian/receipt 篡改、cgroup/container/VM 均留 CP11.4；receipt 与 DB 业务状态对账留 CP11.3c。

## 改动文件

- `meta-research/orchestrator/process_supervisor.py` — 新增：guardian/payload 双层启动、owner-death、subreaper、
  TERM→KILL/reap、durable receipt/recovery、global hard-stop、poison 与 SIGINT 语义保护。
- `meta-research/orchestrator/instance_lease.py` — 修改：spawn-only delegated flock duplicate、at-fork 清理、
  close/信号 mask/异步异常收口。
- `meta-research/orchestrator/runner.py` — 修改：regular/tool-free Codex 统一接 supervisor；timeout/cleanup receipt
  上浮；tool-free root 权限前置；旧 process-group hard-stop 名称兼容转接。
- `meta-research/orchestrator/harness.py` — 修改：supervised staging 执行、日志 fsync、process pointer、exit
  write-all 与原子提升。
- `meta-research/orchestrator/manifest.py` — 修改：向 harness 透传 shared supervisor 与 execution context。
- `meta-research/orchestrator/attack_stages.py` — 修改：smoke/train/eval 注入 context，并强制显式 owner guard 与
  同 owner fenced supervisor 绑定。
- `meta-research/orchestrator/import_worker.py` — 修改：import smoke/eval 接 supervisor；显式 owner guard 拒绝
  standalone/异 owner supervisor。
- `meta-research/orchestrator/run.py` — 修改：lease 后、DB/connector 前恢复 execution receipts；默认装配共享
  supervisor；System 在 DB/lease 前收口 guardian。
- `meta-research/README.md` — 修改：execution receipt 运维面、关闭顺序、root/procfs/trusted/no-cgroup 与跨节点
  边界。
- `meta-research/tests/test_process_supervisor.py` — 新增：双 fork/setsid、timeout、lingering descendant、
  ECHILD、owner SIGKILL、双 guardian 最后 fence、PDEATH pipe 窗口、ambiguous Popen、异常 guardian、SIGINT、
  pass_fds 与 receipt recovery 反例。
- `meta-research/tests/test_instance_lease.py` — 修改：delegated fence signal mask、setup/cleanup 异常回归。
- `meta-research/tests/test_runner_usage.py` — 修改：fake supervisor 注入、全局 hard-stop 与 receipt failure 分类。
- `meta-research/tests/test_obs_parser.py` — 修改：harness receipt/pointer/timeout/short-write 回归。
- `meta-research/tests/test_import_worker.py` — 修改：standalone、异 owner 与同 owner fenced binding 回归。
- `meta-research/tests/test_attack_advance.py` — 修改：AttackStages owner binding 回归。
- `meta-research/tests/test_run.py` — 修改：全量时序下 console/connector 通用入站阻断文案的稳定断言。

## Review（codex-chatgpt gpt-5.5/xhigh）

- 内部三路：core、integration、reference 最终均无 BLOCKER。过程中发现并修复注册后异步异常空窗、
  non-root tool-free signal 权限、guardian 保留 `pass_fds`、waitpid 排空权威、异常 guardian/ambiguous spawn
  poison、SIGINT 自定义/ignore/default/self-replace 语义、pointer 覆盖原错、Import owner 绕过、双 guardian
  最后 fence、mask restore 与 setup 主因优先级。
- 第 1 轮入口：首选 `codexro-review` 的独立账号 refresh token 失效（HTTP 401）；同轮 SSE fallback 的 API key
  亦 401，均未产生模型 verdict，不伪装为通过。
- 第 2 轮（上限）：root ChatGPT 凭据成功，返回 `REQUEST_CHANGES`（`/tmp/cp113b-review-round2.md`）。
  - 采纳：AttackStages 显式 owner guard 也强制同 owner fenced supervisor；exit 侧车改 write-all，并补回归。
  - 未采纳 BLOCKER：称 `_KIND_RE` 不接受 `_`，但实际正则为 `^[a-z][a-z0-9_.:-]{0,63}$`，字符类明确包含
    underscore；`log_name/cycle_id/build_target_id/run_id/...` 已被 supervisor、harness、attack/import 测试真跑。
  - 未采纳 SHOULD：建议将普通 helper `Popen` OSError 视为“确认未 spawn”。已有参数化反例让 wrapper 在真实
    spawn 后抛 OSError/KeyboardInterrupt；仅凭异常类型无法证明没有 guardian，故保留 prepared + poison + fence
    的重型 fail-closed，并补注释说明。
- 已到两轮上限，按 CLAUDE.md §2.2 不发第 3 轮；反馈处置后由本地定向验证收口。

## 验证

- 开发期按用户要求只跑相关验证：
  - supervisor `21 passed`；harness+import `32 passed`；runner `30 passed`；manifest+import `75 passed`；
    attack `52 passed`；run+lease `83 passed`。
  - 最终 owner binding/mask/SIGINT 收口：focused `2 passed`，supervisor+lease `62 passed in 17.15s`。
  - 外审反馈处置：focused `2 passed`；run `45 passed`；attack `53 passed`。
- GPFS/VEPFS 同机 canary：
  `test_owner_sigkill_guardian_holds_flock_until_tree_and_receipt_drain --basetemp=.cp113b-vepfs-canary` →
  `1 passed in 1.60s`；`/vepfs-mlp2/...` 实际挂载为 `gpfs fs_vepfs-cnbj2c98dea54433`。该证据不替代两节点验收。
- `python -m py_compile`（本检查点模块）、`git diff --check`、`git diff --cached --check` 均通过。
- 按用户要求只运行一次全量：
  `PYTHONPATH=meta-research python -m pytest -q meta-research/tests` →
  **`1 failed, 1209 passed in 269.66s`**。唯一失败
  `test_console_backlog_over_one_bounded_batch_blocks_before_later_pause` 在全量时序下得到语义正确的通用
  `人机入站待处理/故障（等待下轮重试）`，测试只接受 `控制台入站待处理`/pause；同一 `test_run.py` 在全量前
  已 `45 passed`，失败后该用例隔离复跑 `1 passed in 0.52s`，证明是 pump 时序分支的断言假阳性。断言改用稳定
  子串 `入站待处理` 后定向 `1 passed, 44 deselected in 0.66s`。依用户要求未运行第二次全量，未伪报全量全绿。
- 结论：功能与相关故障路径验证通过；唯一全量的非功能时序断言已定向修复并如实保留证据。

## 遗留 / 回退

- CP11.3c：plan target `critical/budget_estimate`、current goal lineage、runner/evaluation heartbeat，以及
  timeout/owner_lost receipt 到 `runner_call`/run/attempt/target 的权威 DB 对账、重试和通知。
- CP11.4：调用意图/成本补账、hostile same-UID workload、cgroup/container/VM、guardian/receipt 防篡改与
  content-addressed artifact store。Import `fetch` provider 与默认 import resumer 仍未生产装配。
- 跨节点 VEPFS flock 必须在真实两节点/挂载参数上做同时 acquire 验收；本轮只证明同机 GPFS 路径。
- 回退：先停止所有 orchestrator、connector、guardian 与 payload（Git revert 不会杀正在运行的新版本进程），
  确认 `state/executions/` 无 running receipt 并备份 DB/staging，再执行 `git revert 2f4a5d5`。本提交无 DDL
  migration；旧版本会忽略 receipt 文件，但不能安全接管仍活着的 guardian/子树。
