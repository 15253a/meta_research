# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-09 ｜ 位置：**步⑨（M8）CP9.2**（人类控制台前端接入）
- 检查点状态：CP9.1 已提交（295f84d + build_log 0042）。测试基线 **638 绿**（CP8.8 基线 625 + console 13）。

## 正在做什么
**步⑨（M8）人类控制台接入**（用户 2026-07-09：人类控制台原型没接入系统，最终要系统在控制台上查看；
纠正 CP8.8 期「系统无 web 组件」结论）。原型 = `reference/人类控制台原型-v2.html`。
- CP9.1（295f84d）**控制台数据面已落**：`orchestrator/console_server.py` 独立只读进程——/api/db 真表动态列
  投影 + 派生对象（status_card/live/notification/policy/FS）/ /api/file 白名单虚拟根读 / /api/message spool
  入站。**单写纪律铁律不破**（mode=ro 读 + 零 DB 写；入站只写 <work>/state/console_inbox.jsonl）。

## 下一步动作（按序）—— CP9.2 前端接入
1. `views/console/index.html` 由 `reference/人类控制台原型-v2.html` 派生（reference 原件不动，拷到 views/
   console/ 再改）：
   - 剥 mock：`const DB = {…}` 常量 → loader `fetch('/api/db')` 轮询（如每 3s）填充；`SCENARIOS` 假 live 条
     → 真 payload.live；`FILE_CONTENT` → `fetch('/api/file?p=…')`；假事件 ticker（setInterval streamTick）
     → 真轮询 payload.notification 增量。
   - **形状适配层**：server 是 `payload.tables.<表>`（数组）+ 派生平铺顶层；原型渲染码访问顶层 `DB.<表>` +
     `DB.status_card`/`DB.budget`/`DB.runner_call_live` 等。写一个 `adaptPayload(p)→DB` 把 server 形状映射成
     原型 DB 形状（tables 摊平到顶层 + 字段名对齐：decision.summary←payload_json 截断 / directive.payload←
     payload_json / baseline.tags←baseline_tag join / execution_log.owner←run_id?attempt / live.runner_call→
     runner_call_live / budget←status_card.budget 或 policy）。渲染码尽量不动（保真）。
   - cmdin/narrin 提交 → `POST /api/message {text}`。
2. **验证**：原型自带 HEADLESS node 冒烟（`const HEADLESS=(typeof window==='undefined')`）——可用 node 加载
   改后页 + 喂一份 assemble_db 真产的 payload，断言 adaptPayload + 关键渲染函数不炸（无 window 依赖的纯数据
   路径）。前后端形状契约测试（test：assemble_db 产的键 ⊇ adaptPayload 消费的键）。
3. 内审(Opus) → codex 外审(≤2轮) → 提交 → build_log 0043。
4. CP9.3 入站闭环：run 进程 precheck 边界 ingest console_inbox.jsonl（读未消费行→InteractionIngest.inbound→
   Console.handle_inbound 分类落 directive/message；query→Mediator.handle_query 应答；已消费行游标持久化，
   幂等）。装配进 run.py（make_advancer_precheck 或独立 ingest 步）。pause/resume/query 端到端测试。
5. CP9.4：真 Codex 跑 + console_server 并行 + 浏览器/HEADLESS 真查看实测 + README 控制台节 + 步级验证收口。

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
- 测试基线 **638**。真 Codex 冒烟需代理 7890。
