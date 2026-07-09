# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步⑨（M8）CP9.4**（端到端验收，步⑨收尾）——**空闲/待开工**
- 检查点状态：CP9.3 已提交（**6967dc3** + build_log 0044）。测试 **662 绿**（642+ingest 20）。

> 步⑨进度：CP9.1 数据面（console_server）+ CP9.2 前端真数据 + **CP9.3 入站闭环（ConsoleInboxIngest）已落**。
> CP9.3 经内审(Opus)+外审(codex 两轮，§2.2 修毕)收敛出 no-loss/no-dup 不变量：query-once 落持久层（查 reply 存在性）、
> **只有 durable reply 才推进游标**、line-index 游标、有限重试(5)+终态回执、顶层兜底不崩主循环。剩 CP9.4 收尾步⑨。

## 正在做什么
**空闲 / CP9.4 待开工（步⑨收尾）。** 步⑨（M8）人类控制台接入（用户 2026-07-09：要系统在控制台上真查看）已落三关：
CP9.1 数据面（console_server 独立只读 /api/db + spool 入站）、CP9.2 前端真数据（views/console/index.html 全真化）、
CP9.3 入站闭环（ConsoleInboxIngest：precheck 边界 ingest console_inbox → handle_inbound/mediator）。CP9.4 收尾即完成步⑨。

## 下一步动作（按序）—— CP9.4 端到端验收（步⑨收尾）
1. **真查看实测**：起 run（真 Codex，代理 7890）+ 并行起 console_server（`python -m orchestrator.console_server
   --system-root . --work-root <同 run 的 work> --port 8765`）→ 浏览器/HEADLESS 打开 `views/console/index.html`
   （或 console_server 静态托管的 /）→ 核 /api/db 真数据在页上渲染、/api/file 白名单、控制台发命令→console_inbox
   →run precheck ingest→生效/应答 全链真跑一遍留证。
2. **README 控制台节**：补运维手册——如何起 console_server、如何查看、入站命令闭环（pause/resume/query）、单写纪律边界。
3. **步⑨步级验证收口**：跑步⑨「验证方法」（用户给的那条），留输出作 build_log 步级证据。
4. 内审(Opus) → codex 外审(≤2轮) → 提交 → build_log 0045。
5. 步⑨完成后：回看存量 CP8.6b（eval/import/route，非阻塞）+ 运维执行/硬化（§7.4 T1/T2、双模 A/B、成本账网）。

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
