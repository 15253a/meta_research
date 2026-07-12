# 0079 · CP11.4c.3c.2b.1 五阶段共享文件系统 canary

- date: 2026-07-12
- commit: `fb65955f079349f82da4ed94c06ba32f9691d7e5` — `feat: add CP11.4c.3c.2b.1 shared-fs canary`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3c.2b.1（属：步⑪ CP11.4c.3c.2 目标 VEPFS 验收）

## 决策

只增加一个前台、one-shot、固定五阶段 canary，不增加 daemon、第二 DB、远程执行器、scheduler、
通用 workflow engine 或分布式心跳状态机。远端启动仍由操作者负责：

1. `holder_lease`：holder 的 exact owner child 取得 `InstanceLease`；wrapper 另以父死 pipe 保证自身
   被杀时不能留下孤儿 owner/lease。
2. `contender_ready`：contender 必须先得到绑定 exact owner 的 `InstanceBusyError`，并在 owner 触碰
   SQLite 前 pin 住 artifact FD。
3. `crash_ready`：owner 提交基线、启动既有 guardian，并以小 cache + bounded dirty rows 强制形成
   真 hot rollback journal；DB dirty hash、journal magic/页参数均在 SIGKILL 前机械验证。
4. `holder_complete`：wrapper SIGKILL+waitpid exact owner，随后 rename/replace artifact path；只有 owner
   cleanup 完成后才发布 terminal receipt。
5. `contender_complete`：在 owner death 后至少实际观察一次 guardian delegated fence Busy，写有界
   observation 让 guardian drain，取得新 generation 后验证 terminal receipt、hot rollback、schema/
   quick/FK、FD 原 bytes 与 path-binding fail-closed；资源全部关闭后才发布 terminal receipt。

`local` 使用同一协议的两个真进程，但 immutable scope 固定为 single-node prerequisite，永远不能升级为
`two_node_verified/shared_fs_ready`。默认 `verify` 只在 machine/boot 均不同、挂载为 GPFS、journal 为
`DELETE` 且五阶段闭合时通过；process SIGKILL canary 始终声明
`infrastructure_fence_verified=false`，不冒充 STONITH/partition 验收。

## 改动文件

- `meta-research/orchestrator/shared_fs_canary.py` — `local/node/verify` CLI、五阶段 canonical/no-clobber
  receipt、wrapper 父死 guard、guardian fence observation、真 hot-journal crash/recovery 和严格 scope。
- `meta-research/tests/test_shared_fs_canary.py` — 真双进程/SIGKILL/guardian/SQLite/FD 主链，root/scope/
  no-clobber/双 owner/spawn gap/wrapper death/cleanup final 等负例，并有 portable DELETE hot-journal probe。
- `meta-research/README.md` — 同机和两节点操作命令、结果字段、5 秒 observation latency SLA 与
  STONITH 诚实边界。

## Review（codex-chatgpt gpt-5.5/xhigh）

- 内部审查先后发现并修复：holder wrapper death 留孤儿 owner、CLI 非 JSON 错误、跨 GPFS timing
  假阴性、LOCAL scope 可升级、terminal 先于 cleanup、事务未真正形成 hot journal、guardian delegated
  fence 未被 contender 实际观察、local 第二次 spawn 失败泄漏首进程等问题。
- 据反馈将九阶段草案压成固定五阶段；最终 `canary_code_review` 实跑并终审确认无剩余 BLOCKER 或
  correctness Major。外围 receipt/identity 代码仍偏 verbose，后续如精简必须作为行为不变的独立维护，
  不再增加 phase。
- 外审第 1 轮：`codexro-review` 全新 ephemeral 会话，独立凭证 HTTP 401，无 verdict（现场
  `/tmp/codexrev.17B4eN`）。
- 外审第 2 轮（上限）：同样 HTTP 401，无 verdict（现场 `/tmp/codexrev.r01Ovp`）。
- 依 CLAUDE.md §2.2 不发起第 3 轮；两轮都是鉴权失败，没有未处置的外审代码意见。

## 验证

- 命令：`python -m pytest -q --basetemp=.test-tmp/cp114c3c2b1 tests/test_shared_fs_canary.py`
- 关键输出：`17 passed in 4.88s`。当前 GPFS 上的真实证据包含 DB dirty hash 变化、journal magic
  `d9d505f920a163d7`、SIGKILL 后 DB hash 精确恢复至 baseline、journal 删除、post-kill Busy ≥ 1。
- 命令：`python -m pytest -q --basetemp=.test-tmp/cp114c3c2b1-related tests/test_database.py tests/test_instance_lease.py tests/test_process_supervisor.py tests/test_artifact_capability.py`
- 关键输出：`85 passed in 32.96s`。
- 命令：在 `/tmp` 新绝对目录运行 `python -m orchestrator.shared_fs_canary local ...`
- 关键输出：exit 0；本地 WAL 路径 `status=passed`，同时保持 `two_node_verified=false`、
  `shared_fs_ready=false`。
- `python -m py_compile orchestrator/shared_fs_canary.py tests/test_shared_fs_canary.py`、CLI `--help`、
  `git diff --check` / staged check 均通过。
- 结论：相关验证通过。依用户要求，中间检查点未跑全量；全量只留最终验收。

## 遗留 / 回退

- 当前只有一个实际节点；未把同机两个进程写成两节点通过。目标 VEPFS 上仍须由不同 machine/boot
  各跑一个 role，再执行默认 verifier。
- canary 的 5 秒默认/至少 1 秒 guardian observation 是明确 latency SLA；目标盘更慢时两个角色须使用
  相同且小于总 timeout 一半的更大 grace。超时只会 fail closed，不会假通过。
- 只验证 owner process crash；网络分区、旧 VM 仍活、基础设施 STONITH、正向 GPU、真实 ≥200 轮、
  fault schedule 与最终全量均未完成。
- 回退：`git revert fb65955f079349f82da4ed94c06ba32f9691d7e5`。
