# 0051 · CP11.2b.1–2 耐久鉴权控制面

- date: 2026-07-10
- commit: 10215db8a07ef342d0c9fbf173af18c7291c1674 — feat: 闭合耐久鉴权控制面
- branch: fix/architecture-hardening-20260709
- 检查点 / 步: CP11.2b.1–2（属：步⑪ 生产硬化 · CP11.2 人类控制闭环）

## 决策

把原拟分开的“耐久动作内核”和“鉴权常驻控制台”合并为一个可独立回退的功能提交。原因是外审确认：
只落 reader/action core、却不把生产 `run.py` 与 HTTP writer 接到同一协议，会让测试内路径通过而真实入口仍可
越过未处理控制动作，或根本无法执行 confirm/reject/resolve/cancel；若只补动作端点而不同时加鉴权，又会扩大
未授权控制面。

本检查点因此闭合整条链：

- loopback + Host/Origin + 持久 Bearer capability 的 HTTP 面，研究库连接保持 `mode=ro`；
- `/api/message`、`/api/directive`、`/api/file-request` 统一经稳定跨进程锁与随机幂等键追加到 durable spool；
- byte-offset cursor 绑定 inbox inode/内容 anchor，retry sidecar 原子持久化；撕裂尾、坏行、重放和撞键都有明确语义；
- run 单写进程注入 `FileRequestService` 与 `system_root`，按 operation domain 重核 provenance，执行
  confirm/reject/resolve/cancel；spool backlog、cursor/retry I/O 故障在任何 provider 调用前 fail closed；
- 默认 CLI 常驻等待 pause/文件请求并继续消费动作；`--once` 才是显式一次性模式；
- `abort_cycle` 原子释放 active question；状态漂移时终态拒绝而不半写/反复裸崩；
- `note` 和 reasoning-start 控制输入有界进入同轮 ContextPack；第 129 条在消费前可审计拒绝，且该上限不阻断
  pause/resume/abort 等 operational control；
- `set_budget`、`reprioritize`、`goal_amend` 在真实语义尚未接线前明确 rejected，禁止伪报 consumed；
- 前端只在 fresh snapshot 下开放动作，token 只从 fragment 引导到本标签页 `sessionStorage`；普通断线不回退 mock，
  demo 只在显式 `?demo=1` 下运行；live/ledger/文件读取均有有界、fd-based 投影。

## 改动文件

- `orchestrator/console_spool.py`、`console_ingest.py`、`interaction.py` — durable spool/cursor/retry、文件目录 capability、
  operation-domain 幂等与动作终态收敛。
- `orchestrator/console.py`、`compiler_sqlite.py`、`resource_limits.py` — directive provenance、诚实消费语义、abort 释放、
  reasoning 控制输入共享上限。
- `orchestrator/console_server.py`、`run.py`、`notify.py` — 鉴权 HTTP→spool→run 单写生产装配、常驻等待与通知扫描。
- `views/console/index.html`、`README.md` — fresh-snapshot 动作门、capability 使用与运维边界。
- console/ingest/server/frontend/run/notify/compiler 的回归与浏览器 smoke — 覆盖并发、重放、故障、鉴权、路径与全链动作。

## Review

- 外审第 1 轮：`REQUEST_CHANGES`。两个 BLOCKER：普通入站在 message 落库前持续失败时终态化缺 operation domain；
  129 条 reasoning directive 会先 consumed 再令 compiler 永久失败。一个 SHOULD：abort 遇 active pointer 漂移会反复裸崩。
  三项均修复并加回归。
- 外审第 2 轮（最后一轮）：`REQUEST_CHANGES`。三个 BLOCKER 均为 staged-only 装配缺口：生产 precheck 未消费
  `has_pending`、未向 ingest 注入文件请求服务/系统根、真实 console server 仍用顺序 nonce 的旧 spool 且无结构化动作端点。
- 按 `CLAUDE.md` 两轮上限，不启动第 3 轮；将检查点扩展到安全的鉴权 HTTP 全链，三项全部本地修复，并重新创建
  Git index 隔离工作树跑全量。另自行修正“reasoning 上限可能拒绝 resume”的边界：只对
  `consume_at=reasoning_start` 计数和渲染，operational controls 永远可用。
- 外审证据：`/tmp/codexrev.GAM4M4/verdict.md`、`/tmp/codexrev.y3H5Y1/verdict.md`。

## 验证

- 第 1 轮修复后的 Git index 隔离快照：定向 `197 passed in 36.25s`；全量 `898 passed in 174.88s`。
- 最终扩展检查点定向：8 个 console/server/frontend/run/notify/compiler 文件联合 `292 passed in 70.69s`。
- 最终 Git index 隔离快照全量：`pytest -q` → `959 passed in 191.99s`。
- `git diff --cached --check` 通过；功能提交未混入 build_log/implement_note 记账文件。
- 结论：**通过**（外审两轮上限后，所有已报 BLOCKER 已本地修复并以 staged-only 全量验证）。

## 遗留 / 回退

- CP11.2b.3 仍需落动态 `set_budget`、`reprioritize`、`goal_amend` reasoning-only 路由、真正只读 Codex query
  responder 与真实 connector 投递；完成前 CP11.2/CP11.2b 不勾总完成。
- CP11.3 仍负责 single-instance lease、进程组终止、critical/budget_estimate 与严格 goal 版本隔离；CP11.4 仍负责
  调用意图/回执补账、容器/VM 隔离与内容寻址 artifact store。因此本提交仍不是“数百轮生产验收完成”。
- 代码回退：`git revert 10215db`。本提交无 DB migration；回退会同时撤销新 HTTP 协议和新 spool 读取协议，避免只回退一侧。
