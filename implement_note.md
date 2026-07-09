# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步⑨（M8）CP9.3**（入站闭环）——**空闲/待开工**
- 检查点状态：CP9.2 已提交（**302261a** + build_log 0043）。CP9.1（295f84d + 0042）。测试 **642 绿**（638+前端4）。

> CP9.2 收口：外审第1轮 codex REQUEST_CHANGES（2 BLOCKER[r2 硬编码 / fs 树未接] + 6 SHOULD + 1 NIT）→全改；
> 第2轮 **APPROVE**（余 2 SHOULD[refreshDB 跳拍防并发 / fsOpen 保留 demo 回退] + 2 NIT[session_max=null 显关闭 /
> clampSelections 用 max id] 均已补齐复验）。前端只读观测面全真化 + 稀疏/空/null 守卫，单写纪律不破。

## 正在做什么
**空闲 / CP9.3 待开工。** 步⑨（M8）人类控制台接入（用户 2026-07-09：要系统在控制台上真查看；纠正 CP8.8
期「系统无 web 组件」结论）。原型 = `reference/人类控制台原型-v2.html`。已落：CP9.1 数据面（console_server.py
独立只读 /api/db + spool 入站）、CP9.2 前端接入真数据（views/console/index.html 全真化 + 去 mock 渲染码）。
剩 CP9.3 入站闭环 + CP9.4 端到端验收。

## 下一步动作（按序）—— CP9.3 入站闭环
1. run 进程 precheck 边界 ingest `<work>/state/console_inbox.jsonl`（console_server 的 /api/message 已把人工
   入站写进该 spool，一行 JSON `{connector:console, raw_text, seq, idempotency_key}`，换行终止=committed）：
   读未消费行（游标持久化、幂等）→ `InteractionIngest.inbound(connector, raw_text, idempotency_key, goal_id,
   goal_ver)` → interaction_message；`Console.handle_inbound` 分类 → directive（落库）/ 回执；
   query → `Mediator.handle_query(message_id)` 应答。（M5 入站链已存在，见下「关键上下文」。）
2. 装配进 run.py（advancer precheck 或独立 ingest 步）；ingest 按 UNIQUE(connector,idempotency_key) 去重重放。
3. pause/resume/query 端到端测试（控制台发命令 → run ingest → 生效/应答）。
4. 内审(Opus) → codex 外审(≤2轮) → 提交 → build_log 0044。
5. 之后 CP9.4：真 Codex 跑 + console_server 并行 + 浏览器/HEADLESS 真查看实测 + README 控制台节 + 步⑨步级验证收口。

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
