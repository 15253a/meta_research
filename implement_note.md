# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步①–⑨全完成**——**空闲**。下一批=存量硬化（用户可定优先级）。
- 检查点状态：CP9.4 已提交（**cd97ee0** + build_log 0045）。**步⑨（M8 人类控制台）达成**。测试 **664 绿**。

> **步⑨完成**：人类控制台真接入——CP9.1 数据面(console_server 只读 /api/db + spool)、CP9.2 前端真数据(去 mock)、
> CP9.3 入站闭环(ConsoleInboxIngest)、CP9.4 端到端验收(test_console_e2e 强证零 DB 写 + 入站闭环 + grounded)。
> 系统现可：全研究循环(步①–⑧「正式直接可用」)+ 人类控制台查看真状态 & 交互(pause/resume/query)，单写纪律不破。
>
> **下一批（存量，非阻塞；建议先与用户确认优先级）**：
> 1. **成本记账接线（M6 硬化，最有价值）**：`INSERT INTO ledger`(money) 未接 → `budget.session_max` 安全网休眠、
>    `budget_exhausted` 永不触发（§7/README §6）。真跑长时研究前须接，否则失控成本无自动上限（只 τ + --max-cycles 兜底）。
> 2. CP8.6b：eval target（免训练评估）+ import（外部基线导入）+ route dependency_wait 特化（现遇到干净业务拒、不楔死）。
> 3. worktree 隔离 + env lock 强校验、双模 A/B 会话粒度实测（§7）。
> 4. CP9.3 已知取舍：`_attempts` 不跨重启持久化（注释在案，需要跨重启 liveness 契约时再补）。

## 正在做什么
**空闲。步①–⑨全完成、664 测绿。** 步⑨（M8 人类控制台）本 session 收官：CP9.2 前端真数据 + CP9.3 入站闭环 +
CP9.4 端到端验收 全部提交。系统现可全自动跑研究循环 + 在人类控制台查看真状态 & 交互。等用户定下一批优先级。

## 下一步动作（存量，建议先与用户确认优先级）
1. **成本记账接线（M6 硬化，最有价值）**：`INSERT INTO ledger`(money) 未接 → budget.session_max 安全网休眠、
   budget_exhausted 永不触发（真跑长时研究前须接，否则失控成本无自动上限）。
2. CP8.6b：eval target（免训练评估）+ import（外部基线导入）+ route dependency_wait 特化（现干净业务拒、不楔死）。
3. worktree 隔离 + env lock 强校验、双模 A/B 会话粒度实测（§7）。
4. CP9.3 已知取舍：`_attempts` 不跨重启持久化（注释在案）。

## 关键上下文 / 坑（新 session 不读会踩的）
- **⚠ 开工先看 `git status`**（CP9.1 教训，build_log 0042 载）：工作区若带未提交改动（本次是一份误带的
  CP8.8 no-wedge 反向改动），`git add -A` 会误 staged 进你的提交。核 `git diff --staged --name-only` 只含
  本检查点文件。
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；外审 diff 排除记账类。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- console_server 关键事实：
  - 起服务：`python -m orchestrator.console_server --system-root . --work-root <同 run 的 work> --port 8765`。
  - /api/db 形状：`{tables:{<表名>:[行dict…]}, status_card, live, notification, ledger_by_cycle, policy, fs}`。
    真表投影动态列（PRAGMA），高基数表（execution_log/ledger/runner_call/decision/metric_result/…）id DESC
    LIMIT 500。派生对象各字段见 console_server.py 内 _live/_notifications/_fs_tree。
  - /api/file?p=<虚拟根>/… 白名单：work/ · schemas/ · prompts/ · policies/ · input/（虚拟根显式映射真目录）。
  - /api/message POST {text} → 写 <work>/state/console_inbox.jsonl（connector 固定 console；seq 单调；不碰 DB）。
  - 原型 DB 表字段清单（照真 DDL 画）可用 node 从原型脚本提取（见本 session 勘察）；server 用真列名投影。
- M5 入站链（CP9.3 用）：InteractionIngest.inbound(connector,raw_text,idempotency_key,goal_id,goal_ver) →
  interaction_message；Console.handle_inbound 分类→directive/回执；Mediator.handle_query(message_id) 应答。
- 测试基线 **642**（638 + 前端 4）。真 Codex 冒烟需代理 7890。
- 控制台前端（CP9.2，build_log 0043）：`views/console/index.html` 已全真化；调试用 node 冒烟遍历全 9 标签页——
  `python -c` 造 assemble_db payload（seeded/空库/null-cap）→ `node tests/console_smoke.js <页> <payload.json>`，
  期望 `SMOKE_OK ... tabs=9`。去 mock 原则：**自动渲染面一律读真 payload**；mock DB/SCENARIOS/FILE_CONTENT/classify
  仅离线 demo 回退（连线后 refreshDB/sendCmd 覆盖绕过）。原型原件 `reference/人类控制台原型-v2.html` 不动，diff 它=真决策面。
