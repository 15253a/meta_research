# 0082 · CP11.4c.3d.1 production non-root tool-free Runner

- date: 2026-07-13
- commit: `423dd782fc0c973da7fcd120c1f2341960ee86bd` — `fix(runner): allow hardened tool-free workers under non-root service`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3d.1（属：步⑪ CP11.4c.3d 目标生产运行）

## 问题与决策

生产 preflight 明确要求 orchestrator service 不是 root，但 `interaction-query`、
`adapter-generation`、`adapter-review` 复用的 `CodexRunner(tool_free=True)` 原先又拒绝任何 non-root
调用者。这会产生一个根本装配矛盾：production 可以通过启动身份检查，却会在第一次 query/import
adapter 调用时失败。

采用最小双路径修复，不增加新 daemon、数据库、scheduler、分布式状态或通用 workflow：

- production non-root service 只能以当前 effective UID 运行 tool-free 工人，不允许无特权跨 UID；
- root 开发环境仍必须降到 `codexro` 或显式不同的非 root UID，绝不回退到 root；
- 两路都在新建的空 0700 临时 cwd 执行，经同一个 execution guardian 管理整棵子进程树；
- 冻结 tool policy、UID isolation、run-as、CLI、model、effort 为 runtime contract，responder 与实际
  Runner 在每次执行前精确对账；缺失、非 mapping 或漂移均 fail-closed。

## 能力边界

- 继续用 CLI 参数关闭 web search、shell/unified exec、apps、browser/computer、image、multi-agent、
  MCP elicitation、permission/network proxy 等能力，并对 JSON event/item 使用 allowlist。
- non-root same-UID 子进程只得到 `PATH/LANG/LC_ALL/CODEX_HOME/HOME/TMPDIR` 与代理/TLS 白名单。
  root 跨 UID 路径使用 `sudo ... env -i ...`；外层 sudo 进程也只获得固定的 PATH/locale。
- 输出从临时目录复制前仍核 exact UID、regular file、单链接、`O_NOFOLLOW`、大小上限；writer 副本为 0600。
- `_GuardedRunner` 只透传真实 inner contract，不伪造默认值。测试注入 Runner 必须显式声明匹配契约，
  这不会给生产路径增加兼容性后门。

## 改动文件

- `meta-research/orchestrator/runner.py` — root/non-root 身份解析、冻结 contract、same-UID 启动、
  环境白名单与跨 UID `env -i`。
- `meta-research/orchestrator/mediator.py` — prompt version 绑定 runtime contract，并在执行前严格对账。
- `meta-research/orchestrator/run.py` — guardian wrapper 透传 contract。
- `meta-research/tests/test_runner_usage.py`、`test_query_responder.py`、`test_run.py` — UID、环境、
  contract 缺失/漂移及生产装配回归。
- `meta-research/README.md` — 新环境变量和两种 UID 语义的运维说明。

## Review

- 初轮内部审查在 root self/fallback、构造时环境漂移、wrapper contract 透传、same-UID guardian/output
  约束上逐项收紧；阶段终审无 BLOCKER/Major。
- 外审第 1 轮 `codexro-review` 返回 HTTP 401，无 verdict。
- 外审第 2 轮（上限）`codex-chatgpt` 给出 REQUEST_CHANGES：两个 BLOCKER 是缺失 Runner contract
  被兼容放行、跨 UID `env` 未加 `-i`；一个 SHOULD 是 prompt-version 测试隐含 root 假设。
- 三项均已修复：contract 必须为 Mapping 且完全相等；实际 argv 锁定 `env -i`；测试改为注入身份
  contract 变化，不依赖当前 OS UID。依治理规则不发第 3 轮。最终内部只读复核 APPROVE。

## 验证

- `python -m py_compile`（runner/mediator/run 与相关测试）通过；`git diff --check` 通过。
- 外审边界精确回归：`8 passed in 5.89s`。
- `tests/test_runner_usage.py tests/test_query_responder.py`：`71 passed in 64.47s`。
- 严格 contract 使 3 个旧装配 fake 按预期先失败；测试桩显式声明契约后：`3 passed in 7.34s`。
- `tests/test_repository_adapter_generation.py tests/test_deployment_preflight.py`：
  `41 passed in 8.30s`。
- 最终内部复核另跑 11 条定向用例全部通过。依用户要求，中间检查点未跑仓库全量；全量只留最终验收。

## 真实环境审计与遗留

- worktree 位于 GPFS/VEPFS；宿主机能看到 8×NVIDIA A100-SXM4-80GB，且 224G EEG 参考数据存在。
- 当前 Docker root 在 `/ebs/docker`，`/ebs`/root overlay 已满；Docker 以 rootless/fuse-overlayfs
  运行，且无 NVIDIA runtime、cgroup/resource limit。full-attack E2E 在 container create 时因 ENOSPC 失败，和本 diff
  的 Runner/query 路径无关。
- development-only `--once --no-outbound` preflight 可运行，但诚实 receipt 仍为
  `production_ready=false`：缺 service identity/CODEX_HOME 隐私、资源隔离 attestation、目标 work-root
  mount/quota、Docker/GPU runtime、真实 connector 等生产条件。
- 因此本提交只解除代码内 non-root 装配 blocker，不声称已完成两节点、GPU、真实 ≥200 轮、T1/T2、
  故障组合或最终全量。
- 回退：`git revert 423dd782fc0c973da7fcd120c1f2341960ee86bd`。
