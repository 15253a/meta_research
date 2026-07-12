# 0080 · CP11.4c.3c.2b.2 固定线性故障 schedule runner

- date: 2026-07-12
- commit: `aa03a01226e68104012b291e5cebf55346ce70dc` — `feat: add CP11.4c.3c.2b.2 fault schedule runner`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3c.2b.2（属：步⑪ CP11.4c.3c.2 目标 VEPFS 故障验收）

## 决策

增加一个同机、前台、one-shot 的 fixed-linear fault sidecar，不修改或包装 `orchestrator.run`，也不增加
daemon、第二 DB、scheduler、DAG/plugin、任意 shell/signal、SSH 或远程 kill。owner 的启动和重启仍由
操作者/systemd 负责。

v1 只接受全历史唯一的 execution receipt selector
`(execution_kind, db_owner_kind, db_owner_id)`，动作闭合为 `kill_owner` 与
`kill_execution_payload`。runner 在 signal 前完成 stable instance-lock authority、boot/owner/fence、
canonical running receipt、PID/start ticks 与 pidfd 绑定，并在 pin 后及 durable spent 后再次全历史扫描
selector；任何歧义或漂移均不 signal。

状态只含 immutable canonical `schedule → spent → applied → result → final`：

- `spent` 在 SIGKILL 前 no-clobber+fsync，嵌完整 owner metadata 与 running receipt；发现 spent/no-applied
  的恢复永不重发，只记 inconclusive。
- `applied` 只表示 `pidfd_send_signal(SIGKILL)` 被内核接受，不声明 causality/exactly-once；发现
  applied/no-result 或 applied publication gap 时只恢复 aftermath observation。
- payload 成功要求 exact terminal receipt 为 `outcome=exit`、`returncode=-9`、identity 相同且
  group/container drained；owner 成功要求 pinned owner exit、exact `owner_lost` guardian terminal、旧
  generation/delegated fence 已消失。
- verifier 严核字段闭包、hash chain、action-specific evidence、目录白名单与线性前缀；unknown/越序状态在
  第一次 signal 前即拒绝。所有 receipt 保留 `signal_exactly_once=false`、`recovery_verified=false`。

保留 execution-receipt-only 是有意的最小 scope：本检查点用于真实外调期间的 owner/payload crash；
cycle-boundary kill、随机故障、daemon restart 与恢复正确性不由这个 sidecar 冒充。

## 改动文件

- `meta-research/orchestrator/fault_schedule.py` — fixed-linear schedule schema、全 work-root runner flock、
  exact receipt/lease/pidfd authority、spent/applied crash-gap、strict aftermath evidence、run/verify/validate CLI。
- `meta-research/tests/test_fault_schedule.py` — 真 owner/payload SIGKILL、两事件外部重启、selector race、
  Ctrl-C、spent/applied publication gap、零重发、伪造 evidence/unknown state、pid reuse/zombie 等正反例。
- `meta-research/README.md` — selector 已知/运行后才知的双路径操作顺序、canonical schedule 生成、双终端
  命令、退出码、分段 timeout 和同机/STONITH/恢复诚实边界。

## Review（codex-chatgpt gpt-5.5/xhigh）

- 内部设计/安全审查先后发现并修复：spent 只存不可重算 hash、verifier 可接受伪 complete、applied gap
  不续观察、Ctrl-C 被吞、publish-visible 后局部 hash 错链、payload 未限 `outcome=exit`、selector publication
  TOCTOU、signal 前未拒 unknown/越序状态、wall clock 回拨参与 authority、全历史反复 hash capture、
  README 操作顺序不够可执行及多事件未实测。
- 最终两路内部终审均确认无剩余 BLOCKER/Major；一条真实两事件测试证明同一个 runner 可完成
  owner kill → 外部 launcher 新 generation → 不同 selector payload kill。
- 外审第 1 轮：`codexro-review` 全新 ephemeral 会话，独立 reviewer HTTP 401，无 verdict（现场
  `/tmp/codexrev.WkGjmy`）。
- 外审第 2 轮（上限）：同样 HTTP 401，无 verdict（现场 `/tmp/codexrev.vCdqGQ`）。
- 依 CLAUDE.md §2.2 不发起第 3 轮；两轮均是鉴权失败，没有外审代码意见可处置。

## 验证

- 命令：`python -m pytest -q --basetemp=.test-tmp/cp114c3c2b2 tests/test_fault_schedule.py -x`
- 关键输出：`16 passed in 4.66s`。覆盖真实 pidfd owner/payload SIGKILL、guardian drain、两事件外部重启、
  spent/no-applied 130+零重发、applied/no-result 只观察、post-publication durable-chain 恢复、selector
  pre/post-spent 重扫、伪证据与布局负例。
- 命令：`python -m pytest -q --basetemp=.test-tmp/cp114c3c2b2-related tests/test_fault_schedule.py tests/test_instance_lease.py tests/test_process_supervisor.py tests/test_qualification_firewall.py`
- 关键输出：`86 passed in 25.82s`。
- `python -m py_compile orchestrator/fault_schedule.py tests/test_fault_schedule.py`、四个 CLI `--help`、
  `git diff --check` 与 staged check 均通过。
- 结论：相关验证通过。依用户要求，中间检查点未跑全量；全量只留最终验收。

## 遗留 / 回退

- runner 仅支持同一 host/boot/PID namespace/UID 且内核具备 pidfd；它不证明基础设施 STONITH、网络分区
  安全或 signal causality/exactly-once。
- 当前节点仍未具备第二节点、NVIDIA container runtime 与正式 connector/配额环境；目标两节点正向 canary、
  正向 GPU、真实 ≥200 轮、T1/T2 与最终全量尚未执行。
- 下一检查点是 CP11.4c.3c.3 canonical evidence packer + offline verifier，并须在干净 restore 后续跑至少一轮；
  本 runner 的 `recovery_verified=false` 不提前冒充该结论。
- 回退：`git revert aa03a01226e68104012b291e5cebf55346ce70dc`。
