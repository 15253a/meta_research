# 0078 · CP11.4c.3c.2a 共享盘 SQLite 存储模式收口

- date: 2026-07-12
- commit: `75f8009c2f7b9ceb6a7c51c48d238b7b50ea8753` — `fix: close CP11.4c.3c.2a shared SQLite boundary`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3c.2a（属：步⑪ CP11.4c.3c 验收与科学隔离工具）

## 决策

`reference/` 的 SQLite WAL 是单机 embedded 设计，而 production work-root 被固定在 GPFS/VEPFS。
SQLite 上游明确要求 WAL 的全部进程位于同一 host，因此不用一次共享盘 canary 去冒充
上游支持。本检查点选择最小收口：

- 已知本地文件系统继续 WAL；GPFS 和未知文件系统使用 `DELETE` rollback journal +
  `synchronous=FULL`，不新增 DB/server/daemon。
- 共享盘若留有旧 WAL，已持有 `InstanceLease` 的唯一生产进程在任何 schema/data 读取前，
  用 `locking_mode=EXCLUSIVE` 完成 WAL→DELETE，再恢复 NORMAL。
- 共享盘控制台只在已证实本 boot active owner，或明确 inactive+无锁时准入；其余状态在
  `sqlite3.connect` 前 fail closed 并向 HTTP 稳定返回 503。这是请求准入，不是分区 fence。
- 跨节点仅支持旧节点已被基础设施 STONITH 后的 crash-stop 串行接管；不在系统内新造
  分布式 lease/心跳协议。

## 改动文件

- `meta-research/orchestrator/database.py` — 新增有界 mountinfo 解析和保守 journal 选择；在首次库读前
  建立/核验 journal、synchronous 与旧 WAL 迁移顺序。
- `meta-research/orchestrator/deployment_preflight.py` — 将 GPFS 所需 `DELETE` 模式和 single-active-host
  要求写入部署预检投影，不把它表述成实测证明。
- `meta-research/orchestrator/instance_lease.py` — status 投影 owner hostname/boot ID，并在内部派生
  `local_active_owner`，不让 console 自行解释 hostname。
- `meta-research/orchestrator/console_server.py` — 共享盘只读连接增加 fail-closed 准入和专用 503 异常。
- `meta-research/orchestrator/compiler_sqlite.py` — 注释/契约从“所有文件库 WAL”更正为本地 WAL /
  共享盘 rollback 下的 SQLite 一致快照。
- `meta-research/README.md` — 记录 SQLite 上游边界、journal 选择、crash-stop 接管与 STONITH 限制。
- `meta-research/tests/test_database.py` — 覆盖本地/共享/未知文件系统、mount 边界/转义/冲突/
  symlink 解析、旧 WAL 崩溃残留和“EXCLUSIVE 为首条 SQL”顺序。
- `meta-research/tests/test_console_server.py` — 覆盖 invalid/stale 在 open 前拒绝、本机 owner 准入和 GET
  503 不泄身份细节。
- `meta-research/tests/test_instance_lease.py` — 覆盖同 hostname 但不同 boot 不得投影为本机 owner。
- `meta-research/tests/test_deployment_preflight.py` — 锁定 GPFS 的 required journal 投影。
- `meta-research/tests/test_console_e2e.py` — 更新只读连接的 work-root 准入参数和本地 WAL 表述。

## Review（codex-chatgpt gpt-5.5/xhigh）

- 内部终审：
  - `data_abi_review` 先报出“先读 schema、后切 WAL→DELETE” BLOCKER；改为 EXCLUSIVE 首 SQL 并补 trace
    回归后 APPROVE，无剩余 BLOCKER。
  - `core_firewall_review` 先报出 invalid+lock-false 被当成 offline 放行 BLOCKER；改为只允许 local-active
    或明确 inactive 后 APPROVE，定向 6 passed。
- 外审第 1 轮：`codexro-review` 全新 ephemeral 会话，独立凭证 HTTP 401，无 verdict（现场
  `/tmp/codexrev.l2k321`）。
- 外审第 2 轮（上限）：同样 HTTP 401，无 verdict（现场 `/tmp/codexrev.js0S0N`）。
- 依 CLAUDE.md §2.2 不发起第 3 轮；两轮都是鉴权失败，没有未处置的外审代码意见。

## 验证

- 命令：`python -m pytest -q --basetemp=.test-tmp/cp114c3c2a-db tests/test_database.py`
- 关键输出：`20 passed in 12.16s`
- 命令：`python -m pytest -q --basetemp=.test-tmp/cp114c3c2a-runtime tests/test_deployment_preflight.py tests/test_console_server.py tests/test_console_e2e.py tests/test_instance_lease.py`
- 关键输出：`134 passed in 96.82s`
- 终审修正后命令：`python -m pytest -q --basetemp=.test-tmp/cp114c3c2a-final-targeted tests/test_console_server.py tests/test_deployment_preflight.py::test_production_positive_with_zero_requested_gpus tests/test_instance_lease.py::test_real_process_same_root_is_busy_but_different_root_can_run tests/test_instance_lease.py::test_status_does_not_treat_matching_hostname_on_another_boot_as_local`
- 关键输出：`65 passed in 86.69s`
- 命令：`python -m py_compile orchestrator/database.py orchestrator/console_server.py orchestrator/instance_lease.py orchestrator/deployment_preflight.py`
- 关键输出：exit 0；`git diff --check` / `git diff --staged --check` 均通过。
- 结论：相关验证通过。依用户要求，中间检查点不跑全量；全量仅留最终验收。

## 遗留 / 回退

- 未在当前单节点环境虚报两节点通过。CP11.4c.3c.2b 仍需目标 VEPFS 两节点实测
  lease 互斥、guardian/fd、journal 恢复和 crash-stop 接管。
- console 检查是请求准入，不消除 status→open 的 TOCTOU；部署必须在跨节点接管前停止/
  fence 旧节点及其 console。
- 未跑全量、未完成正向 GPU canary、未执行真实 ≥200 轮。
- 回退：`git revert 75f8009c2f7b9ceb6a7c51c48d238b7b50ea8753`。
