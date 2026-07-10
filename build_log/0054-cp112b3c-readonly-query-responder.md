# 0054 · CP11.2b.3c 真正只读 Codex query responder

- date: 2026-07-10
- commit: 92ecf181159682fe4536206f9fe0aabd3b71d005 — feat: 闭合只读查询旁路
- branch: fix/architecture-hardening-20260709
- 检查点 / 步: CP11.2b.3c（属：步⑪生产硬化 · CP11.2 人类控制闭环）

## 决策

生产 query 不再由模板冒充模型回答，也不允许模型生成的自由文本直接进入 reply。独立
`interaction_query` skill 只能从已发布状态卡选择精确 `{path, value}` facts；编排器逐项复核标量值与卡片
完全相等后，才由确定性 renderer 生成用户可见回复。query、有限会话历史和状态卡均先做有界化与凭据消毒，
会话按 connector + 128-bit conversation id + goal id/version 隔离；没有可靠 conversation id 时不复用历史。

查询调用使用与主进程不同的 `codexro` UID、临时工作目录、显式禁用工具的 Codex 配置以及最小文件跨 UID
桥接。状态卡和输出均通过 `O_NOFOLLOW`、fd 类型/属主/硬链/大小检查读取；查询进程组纳入超时、关闭和第二次
Ctrl-C 的 TERM/KILL 注册表。模型无 DB 凭据，也不能把 shell/web/apps/browser 等工具接回查询路径。

外部调用前先耐久落 `created → running` intent；完成时由 WriteDaemon 在一个事务内落 runner_call 用量、成本
ledger 和 reply。调用结果未知时 fail-closed，不自动重放可能已经收费的调用。每个 conversation 使用耐久 FIFO
排队，队列上限 256；spool cursor 可越过已接纳 query 继续处理控制消息，重启后仍按会话顺序恢复，出队前重核
预算和状态卡。常驻 interaction pump 在研究阶段旁路执行，受控退出只排空已接纳工作，不被持续新输入无限延长。

## 主要改动

- `orchestrator/mediator.py`、`interaction.py`：facts-only responder、状态卡/历史消毒、候选复核、确定性渲染、
  runner intent/receipt 与未知调用失败收敛。
- `orchestrator/console_ingest.py`、`console.py`、`console_server.py`、`views/console/index.html`：耐久逐会话 FIFO、
  非阻塞 spool intake、goal/card 绑定及 session-scoped 128-bit 浏览器会话身份。
- `orchestrator/run.py`、`runner.py`、`resource_limits.py`：常驻查询 pump、受控 drain、第二次 Ctrl-C 硬停、
  不同 UID/tool-free Codex 调用、安全文件桥与进程组回收。
- `orchestrator/writedaemon.py`、`cost_ledger.py`、`database.py`、`interfaces.py`：单 writer 跨线程串行化，
  查询用量/成本/reply 原子收尾及运行目录/DB 权限收紧。
- `prompts/skills/interaction_query/SKILL.md`、`schemas/interaction_reply_candidate.schema.json`、
  `schemas/policy.schema.json`：只选事实的模型契约、结构校验和 query policy v3。
- `tests/test_query_responder.py` 及 console/run/runner/ledger/WriteDaemon/schema 回归：覆盖事实旁路、工具与 UID
  隔离、FIFO/重启/预算竞态、坏卡终态、150+ cycle 常驻、drain、超时和硬停。

## Review

- 内审设计、耐久性和安全性三路最终均无 BLOCKER。设计相关定向 `123 passed`；耐久性最终
  `165 passed`；安全性最终 `158 passed`，包含真实 tool trace、不同 UID 和进程组 marker 检查。
- 外审第 1 轮通过 `bin/codex-review.sh` 启动，`codexro` 账号在产出 verdict 前触发用量上限；仅留下
  `/tmp/codexrev.S6TUUG/prompt.txt` 与 `staged.patch`，没有 verdict。
- 外审第 2 轮按 fallback 模式把材料内联交给 `codex-chatgpt exec`，同样在 verdict 前触发账号用量上限，
  没有生成 `/tmp/codex-review-cp112b3c-round2.md`。遵守 `CLAUDE.md` 最多两轮约束，未启动第 3 轮；因此本
  检查点只有内部复核结论，**没有外部 APPROVE**。

## 验证

- 开发期间只跑相关验证；最后一组核心查询/运行加固定向为 `81 passed in 75.59s`，其余设计/耐久/安全定向
  结果见 Review。
- 按用户要求只做一次最终全量：`pytest -q`（workdir=`meta-research/`）→
  `1055 passed, 1 failed in 257.05s (0:04:17)`。唯一失败是
  `test_console_frontend.py::test_page_derived_from_prototype_and_wired`：新 conversation id 初版写入
  `localStorage`，违反既有前端“不用 localStorage/cookie”的安全契约。
- 修复为既有 `sessionStorage` 后只做相关验证：`pytest -q meta-research/tests/test_console_frontend.py` →
  `6 passed in 1.60s`。依用户指示未再跑第二次全量，因此不声称取得新的“全量全绿”结果。
- 提交前 `git diff --check`、`git diff --cached --check` 与
  `python -m compileall -q meta-research/orchestrator` 通过；功能提交未混入 build log/ROADMAP/implement note。
- 结论：**检查点通过（带披露）**。唯一全量失败已由对应回归覆盖并修复；CP11.2b.3/CP11.2 总项仍未完成。

## 遗留 / 回退

- 下一检查点：CP11.2b.3d 真实 connector 投递；其后是 CP11.3/CP11.4 和字面意义的真实 100+ 轮生产验收。
  `heartbeat_ref` 仍为空，当前不能宣称 reference 完整落地或最终生产级。
- 后续安全 SHOULD：约束只含认证材料的 `CODEX_HOME`，固定/记录 CLI 版本身份，并为 trace 增加总字节上限。
- 代码回退：`git revert 92ecf18`。本提交没有 DDL migration；若活库已有 query runner_call/ledger/reply，
  Git revert 不会删除这些耐久记录，回退前应 pause 并保留审计历史。
