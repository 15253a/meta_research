# 0083 · CP11.4c.3d.1.1 tool-free CLI trusted PATH

- date: 2026-07-13
- commit: `6aec82831d658b2c52764c43a44505c7ed665096` — `fix(runner): make default tool-free Codex launcher executable`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3d.1.1（属：步⑪ CP11.4c.3d 基本可用 / 目标生产运行）

## 决策

d.1 把 tool-free worker 环境收紧为显式白名单后，PATH 取 Python `os.defpath`。本机该值是
`/bin:/usr/bin`，而默认 `/usr/local/bin/codex` 是 `#!/usr/bin/env node` launcher：它因此找到旧的
`/usr/bin/node` 12，解析 Codex 0.144.0 时立即 `SyntaxError`；同机 `/usr/local/bin/node` 20 可正常启动。

采用单一常量 `/usr/local/bin:/usr/bin:/bin`，不引入 binary discovery、第二配置层或 launcher wrapper。
该目录集合与现有 sandbox payload PATH 一致，仍不继承调用者 PATH；默认入口本身已位于 root-controlled
`/usr/local/bin`。exact PATH 加入 responder/Runner 严格 runtime contract，tool policy 升 v6，避免旧 prompt
identity 或自定义 Runner 在执行语义变化后被误复用。

## 改动文件

- `meta-research/orchestrator/runner.py` — 固定 root/non-root 两路 worker PATH，contract 增 `exec_path`，
  tool policy v5→v6。
- `meta-research/tests/test_runner_usage.py` — 锁定 root `env -i` argv、non-root process env 与 contract PATH。
- `meta-research/README.md` — 记录默认 query CLI 的固定 PATH/shebang 契约。

## Review

- 内部只读审查 APPROVE：确认 root clean-env 与 `codexro` clean-env 的 `codex --version` 均成功；PATH
  同时进入两种执行分支和 strict contract，未扩大工具/环境能力。
- 外审第 1 次调用在 reviewer 启动前因 wrapper 已内置 model、调用方重复传 `--model` 被参数解析拒绝，
  无 reviewer verdict；按保守口径计一次。
- 外审第 2 次（上限）完整浏览 staged diff、mediator strict equality、wrapper、测试与相关 PATH 用法，
  结论 APPROVE，明确“未发现 BLOCKER/SHOULD/NIT”。未发第 3 次。

## 验证

- `python -m py_compile orchestrator/runner.py tests/test_runner_usage.py` 与 `git diff --check` 通过。
- PATH/root/non-root/contract 精确回归：`3 passed in 2.01s`。
- `tests/test_runner_usage.py tests/test_query_responder.py`：`71 passed in 64.36s`。
- 真实基础 smoke：root development 诊断装配 `build_system(..., attack=False)` 完成 `c1`，runner_call success，provider ledger
  `tokens_total=16100`；状态为 done 并发布 cycle snapshot。
- 为隔离 query 修复且避免重跑 reasoning，从该 c1 snapshot 恢复新诊断 work-root，并用现有 publisher
  重建同一 c1 status card。修复前默认 CLI 为 Node 12 `SyntaxError`；改用原生 binary 后先证明 auth/模型
  链成功，最终用修后代码且**不设置** `METARESEARCH_QUERY_CODEX_BIN`/
  `METARESEARCH_QUERY_CODEX_HOME`：`responder_kind=codex`、interaction runner_call success、
  `failure_kind=NULL`、`tokens_total=9347`。
- smoke work-root/TMPDIR 均已清理。依用户要求，本中间检查点未跑仓库全量；只在最终验收跑一次。

## 环境处置、遗留与回退

- `codexro` 旧 auth 已失效；root overlay 0 bytes 导致第一次本地复制只写出部分文件。已删除该部分文件，
  仅清理本轮/既有 pytest 临时根 `/tmp/pytest-of-root`，获得约 9MB 后从 root 主认证恢复本地 0600 完整副本，
  并以真实 query 证明有效。
- 诊断期间短暂使用的 VEPFS auth 副本已立即删除；共享 VEPFS 不作为长期凭据目录。
- 该 smoke 证明单节点 bootstrap reasoning、snapshot-grounded query、guardian/provider receipt 与 ledger
  基本可用；不证明 full attack/import/training、Docker/GPU、生产 connector、双节点、≥200 轮、T1/T2 或最终全量。
- `/ebs` Docker backing store 仍 ENOSPC，root overlay 仍近满；完整 attack 当前仍是环境 blocker。
- 回退：`git revert 6aec82831d658b2c52764c43a44505c7ed665096`。
