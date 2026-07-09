# 0049 · CP11.2a 用户文件资产接纳与最小授权

- date: 2026-07-09
- commit: c077cd25c16b504a9aa22845e10349f5129eda71 — feat: CP11.2a 闭合用户文件资产接纳与最小授权
- branch: fix/architecture-hardening-20260709
- 检查点 / 步: CP11.2a（属：步⑪ 生产硬化 · CP11.2 人类控制闭环的资产子检查点）

## 决策

把「文件请求已 resolve」从只有 DB 状态的表面闭环，改成可在真实 bundle 执行中消费的、可恢复的最小资产权限：

- 文件只原子发布到 `<work>/input/user_provided/`，通过同 fd 复制/hash、严格 UTF-8 预览与全目录 fsync 闭合 TOCTOU 和崩溃恢复；
- 请求、条目、资产、预览、回执与磁盘占用共享硬上限，在不可变终态前拒绝超额；
- compiler 只渲染 goal-wide 最新终态的有界、非证据回执，不泄露原文件名、托管路径或请求路径；
- manifest 只能使用生成时 ContextPack 已授权的 opaque ref；v2 快照进一步冻结
  `ref/request/item/asset/sha256/size/managed_path`，fresh、resume 与每次启动前均对账；
- 资产通过验证后持续打开的 fd 与 `pass_fds` 交给子进程，不再验后按路径重开；
- 同托管根的 resolve/cancel 用稳定 root-global `flock` 串行，锁覆盖 cleanup→配额快照→复制/发布→DB 终态，同时闭合同请求竞态与跨 goal 磁盘配额穿透；
- 终态入口在任何 attempt 删除前重验冻结请求的 schema、policy 上限与 canonical hash，rollback 清理失败不再遮蔽原始异常，遗留 staging 字节也计入硬配额。

`pack_hash` 保留为生成时包的 ledger 审计锚，不与 resume 的当前 pack 做相等比较：后续 append-only 回执会合法改变当前 pack；恢复时的执法闸是冻结 ref 仍在当前 pack，且内容身份不变。

## 改动文件

- `meta-research/README.md` — 记录用户资产真消费方式和 CP11.4 前的受信任 operator 边界。
- `meta-research/orchestrator/attack_stages.py` — bundle 生成时冻结实际 refs 的内容身份，fresh/resume 复核后传入 smoke/train/eval。
- `meta-research/orchestrator/compiler_sqlite.py` — goal-wide 最新回执、全局 pending 阻断、有界元数据/预览与 opaque ref 编译。
- `meta-research/orchestrator/harness.py` — 将已验资产 fd 安全传给子进程。
- `meta-research/orchestrator/interaction.py` — 收紧文件请求 attempt 的终态重提/去重语义。
- `meta-research/orchestrator/manifest.py` — 实现 opaque asset placeholder、DB/FS/fd 三方对账、v2 授权快照、ledger 保护与 `pass_fds` 执行。
- `meta-research/orchestrator/notify.py` — 实现原子文件接纳、全局配额、root-global claim、耐久化/恢复、终态复验与原异常保真。
- `meta-research/orchestrator/resource_limits.py` — 新增接纳与编译共用的不可变请求/条目/资产/取消理由上限。
- `meta-research/orchestrator/run.py` — 把用户文件托管根从代码根纠正为当前 work root。
- `meta-research/orchestrator/runner.py` — 在所有阶段 prompt 中固定用户回执的 untrusted/non-evidence 守卫。
- `meta-research/prompts/system_prompt.md` — 明确非 bundle 阶段只获得有界预览，bundle 才能按 ref 消费资产。
- `meta-research/schemas/execution_manifest.schema.json` — 允许受控 `{asset:opaque-ref}` 命令占位符。
- `meta-research/schemas/policy.schema.json` — 将文件请求 policy 上限与共享运行时上限对齐。
- `meta-research/schemas/resource_request.schema.json` — 限定请求数组、元数据长度并拒绝 C0/DEL 控制字符。
- `meta-research/tests/test_attack_advance.py` — 覆盖 fresh/resume 真消费、未来可预测 ref 不扩权和冻结身份传递。
- `meta-research/tests/test_compiler_sqlite.py` — 覆盖 goal-wide 回执、路径/元数据清洗、JSON 转义预算和 5 请求/512 资产最大合法状态。
- `meta-research/tests/test_manifest.py` — 覆盖授权快照、fd 保活、DB/FS 绑定、同 ref 自洽改写拒绝和崩溃 fsync 语义。
- `meta-research/tests/test_notify.py` — 覆盖原子接纳、并发 resolve/cancel、跨 goal 配额、commit 歧义、坏 pending、rollback 遮蔽与 staging 配额。
- `meta-research/tests/test_run.py` — 把文件请求 E2E 从「清 pending」升级为真文件进 ContextPack 的完整闭环。
- `meta-research/tests/test_runner_usage.py` — 锁定即使无 resolved refs，untrusted 回执守卫也必须存在。
- `meta-research/tests/test_schemas.py` — 锁定 schema 元数据边界与运行时硬上限一致。

## Review

- 内部构建复核：多轮找出并修正 DB/ContextPack 授权绑定、TOCTOU、fsync、resume 权限扩张、
  goal 资产上限与 compiler 上限错位、JSON 转义膨胀和 prompt 守卫等问题。
- 首选 `codexro-review` 的本机凭证因 `401 refresh_token_reused` 失效，未产生审查结论；按 CLAUDE.md §2.1 降级到模式 B（完整 staged diff 内联、只读）。
- 外审第 1 轮：`REQUEST_CHANGES`。BLOCKER：可预测 ref 未绑生成时 ContextPack/DB；发布缺完整 fsync/DB 崩溃恢复；
  goal-global 去重与局部回执范围不一致。SHOULD：manifest/path TOCTOU、累计磁盘配额、goal 总资产上限。均已修复。
- 外审第 2 轮（上限）：`REQUEST_CHANGES`。BLOCKER：resolve/cancel 无 in-flight claim，可形成「DB resolved 指向已删资产」，且跨请求磁盘配额可并发超额。SHOULD：终态前未复验请求 schema；`pack_hash` 未防同 ref 重写；rollback cleanup 会遮蔽原异常。NIT：崩溃遗留 staging 未计配额。
- 按 CLAUDE.md §2.2 两轮上限，**不开第 3 轮**。上述第 2 轮反馈全部本地核实并修复：
  root-global claim、resolve/cancel 共用终态复验、v2 内容身份快照、primary exception 保真、staging 字节计费均已补回归。
- 修复后三路内部最终只读复核：均 `APPROVE`，无 BLOCKER/SHOULD。
- 外审证据：`/tmp/cp112a-review-round1.md`、`/tmp/cp112a-review-round2.md`。

## 验证

- 第 2 轮补修后子集：
  - `pytest -q meta-research/tests/test_notify.py` → `67 passed in 5.61s`；
  - `pytest -q meta-research/tests/test_manifest.py` → `65 passed`；
  - `pytest -q meta-research/tests/test_attack_advance.py` → `52 passed`；
  - stale cancel / 跨进程 crash-release / 跨 goal quota 三项并发回归连续 5 轮全过。
- 当前工作区核心组合：
  `pytest -q test_notify.py test_compiler_sqlite.py test_manifest.py test_attack_advance.py test_runner_usage.py test_schemas.py test_run.py`
  → `330 passed in 46.63s`。
- Git index 隔离快照同组：`329 passed in 74.92s`；比工作区少的唯一测试为未暂存 CP11.2b 的
  `test_production_system_scans_directive_and_file_notifications_on_exit`。
- **Git index 隔离快照全量**：`pytest -q <snapshot>/meta-research/tests`
  关键输出：
  ```text
  844 passed in 154.77s (0:02:34)
  ```
- `git diff --cached --check` 与三路最终复核均通过。
- 步级验证：CP11.2 尚有 CP11.2b 真实 console confirm/reject/resolve/cancel 控制面，本检查点不收尾整个 CP11.2。
- 结论：**通过**。

## 遗留 / 回退

- CP11.2b：提交已隔离在 unstaged 的 HTTP console→spool→run 单写 ingest→权威终态控制面。
- CP11.4 边界仍在：当前只对受信任 operator input 开放用户资产。Codex 生成代码/prompt 的强对抗隔离、容器/VM 与内容寻址 artifact store 尚未完成。
- 代码回退：`git revert c077cd2`。本提交无 DB migration；若已在运行 work root 生成 v2
  `_asset_authorization.json` 或新式 resolved 资产回执，回退前先备份 work root，并用旧版重建相关 bundle/回复到配套快照，不要让新旧授权格式混用。
