# 0043 · CP9.2 人类控制台前端接入真数据（去 mock 渲染码）

- date: 2026-07-09
- commit: 302261a — feat: CP9.2 人类控制台前端接入真数据（去 mock 渲染码）
- branch: main
- 检查点 / 步: CP9.2（属：步⑨ M8 人类控制台接入——系统在控制台上可查看/可交互）

## 决策
**做了什么**：把人类控制台原型（`reference/人类控制台原型-v2.html`，一个自带 mock 数据、可离线演示的
单文件页）派生成 `views/console/index.html` 并**接上真系统**——数据源换成 CP9.1 落的 `/api/db`，使
「系统在控制台上真查看」（用户 2026-07-09 明确要求）。

**为什么不只是换 DB 常量**：勘察发现原型把 mock 数据**焊进了渲染码本身**（硬编码 `goal v2`/`cycle 19`、
mock `SCENARIOS` live 模型被 10+ 处读、每 5s 编造 loss/watchdog 的假 ticker、buildStreamHistory 里 9 行假
telemetry、vPool 两张假 ppl/degradation 排行、按魔法轮号 `c.id===19/11/6/15/9` 贴死的 6 段演示剧情 + 2 个含
假 NaN 日志的函数、narrator 焊死剧情）。只换 `DB` 常量，这些**渲染码内的假数据仍会显示** → 不满足「真查看」。
本检查点因此把**所有自动渲染面**改成读真 payload、**关掉一切编造**、并对稀疏/空/null 真库加守卫。

**影响面**：纯前端观测面（`views/console/`）+ 两个测试。**不碰后端、不碰编排器**。单写纪律铁律不破：前端只
`GET /api/db`·`GET /api/file` + `POST /api/message`（写 spool，非 DB），零 DB 写。

## 改动文件
- `meta-research/views/console/index.html` — 新增（由 reference 原型派生 + 换数据源 + 去 mock）：
  - 注入「真数据接入」块：`adaptPayload`（server 形状→原型 DB 形状：表平铺顶层 + status_card 嵌套
    (goal/selection/counts/budget)拍平改名 + budget 合成[含 global_remaining 优先真值] + directive
    payload_json 解析成对象 + ledger_by_cycle "c1"→数字[isNaN 才回退保 c0] + fs 树 {p,dir,children}→{p,d}）；
    `buildLive`/`applyLive`（真 payload.live 覆盖 `SCENARIOS[真mode]` 且 `state.scenario=真mode`，10+ 处读
    SCENARIOS[state.scenario] 的渲染码零改即读真值；`LIVE_MODE` 置真 → 隐藏场景切换器/「模拟」按钮）；
    `clampSelections`（mock 默认选中 selB:4/selCycle:19/selA:6/selQ:13 在稀疏真库不存在 → 夹到真 id，
    最新轮取 max id）；`streamSyncReal`（seen-set[键=t|k|text] 每拍重算 buildStreamHistory 全量、只追加未见项
    → 新轮/新决策/新通知都进流、乱序/坏行不重不漏）；`emptyPage`（空表占位）；`refreshDB`（GET /api/db →
    adapt→applyLive→clamp→streamSync→render，3s 轮询，`_dbInFlight` 跳拍防并发堆积；HEADLESS 不 fetch）；
    `fsRefresh`（真模式用 `DB.fs`、未连线 demo 回退 mock `FS`）；`fsOpen`（真模式→GET /api/file、demo→mock
    FILE_CONTENT）；`sendCmd`（POST /api/message，校验 `r.ok && j.queued` 否则走失败路径）。
  - 去 mock 渲染码：renderTopbar/bandHTML（goal/cycle/心跳/预算/全局余/active-question 真化）、
    buildStreamHistory（删 9 行假 telemetry + note/summary 空守卫）、vPool（假 lead1/lead2 排行 → 真
    metric_result join；空池 emptyPage）、vGoal（$85.6/$600、0.74 分、gla-gate 谓词 → 真 budget/score）、
    vLedger（runner_call 卡焊死 #812 → 真 runner_call_live + null 守卫；预算策略 $4/$40/d1 → 真 policy.budget；
    全局判停 session_max=null → 显「关闭」）、vDirectives（真 interaction_request 文件请求 + directive.confirmed）、
    narratorRespond/narratorSeed（进展/预算/文件/开场白 → 真 status_card/budget/interaction_request）；
    删 6 段魔法轮号演示面板 + 2 死函数（c19Detail/obs11Detail）；vCycles/vEvidence 空表 emptyPage + 各处 null 守卫。
  - 保留：mock `DB`/`SCENARIOS`/`FILE_CONTENT`/`classify` = **离线 demo 回退**（未连 server 时页面仍可作演示；
    连线后 refreshDB/sendCmd 覆盖绕过 classify）。
- `meta-research/tests/console_smoke.js` — 新增：node HEADLESS 冒烟。载入改后页（Proxy 假 DOM，不定义 window→
  HEADLESS=true），adaptPayload→applyLive→clampSelections→streamSyncReal→fsRefresh→**遍历全 9 标签页 render**，
  逐页收集失败；断言 budget 派生就位 + 不抛。
- `meta-research/tests/test_console_frontend.py` — 新增：①前后端形状契约（assemble_db 键 ⊇ adaptPayload 消费）；
  ②前端整合标记就位（let DB / adaptPayload / refreshDB / api 端点）；③node 冒烟 seeded 真数据；④node 冒烟**空库**
  （全新 run：零行 + 无 status_card，真开机态）。

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内审（Opus 子代理，构建中）：抓出 adaptPayload 漏 budget → 真数据一刷新 topbar 崩（Cannot read 'B_t'）；
  冒烟未跑「adapt 后 render」路径。均在送外审前修复（补 budget/status_card 拍平 + 冒烟补 render-on-adapted）。
- 外审第1轮：**REQUEST_CHANGES**（2 BLOCKER + 6 SHOULD + 1 NIT）——
  - [BLOCKER] bandHTML 硬编码「等待用户供给 r2」；[BLOCKER] 前端丢弃 /api/db 的 `fs`、文件树仍走 mock。
  - [SHOULD]×6：vLedger 硬编码 B0$4/$40/d1；budget 丢 global_remaining；refreshDB 无 in-flight 保护；
    sendCmd 不校验 r.ok/queued；streamSyncReal 只按 notification 长度增量（漏新 cycle/decision、怕乱序）；
    [NIT] active chip go 不选中问题。**全部已改**（逐条见 commit 与第2轮 prompt）。
- 外审第2轮：**APPROVE**（无 BLOCKER；两 BLOCKER 已解）。余 2 SHOULD（refreshDB 真正跳拍防并发、fsOpen 保留
  demo 回退）+ 2 NIT（session_max=null 显「关闭」、clampSelections 用 max id）——codex 明言「不再阻断 CP9.2」，
  **仍全部补齐**并复验。
- 未采纳意见：无（全采纳）。

## 验证
- 命令：`python -m pytest -q`（含 test_console_frontend.py 4 条）
- 关键输出：
  ```
  642 passed in 108.45s
  ```
- node HEADLESS 冒烟（全 9 标签页 render 不抛）：
  ```
  --- seeded ---        SMOKE_OK tables=34 q=1 dec=1 B_t=40 tabs=9
  --- null session_max ---  SMOKE_OK tables=34 q=1 dec=1 B_t=40 tabs=9
  --- empty ---         SMOKE_OK tables=34 q=0 dec=0 B_t=0 tabs=9
  ```
  （空库=全新 run 真开机态；null-cap=全局预算网关闭；均逐页 render 不抛——去 mock 后自动渲染面在稀疏/空/null
  真数据上不崩、不显编造数据。）
- 单写纪律：前端仅 GET /api/db·/api/file + POST /api/message（写 `<work>/state/console_inbox.jsonl`），零 DB 写。
- 步级验证（步⑨未收尾，CP9.3/9.4 待做）：本检查点不收尾步⑨，故不跑步级验证；步⑨验证方法待 CP9.4 收口时跑。
- 结论：**通过**。

## 遗留 / 回退
- 待办（下一检查点）：
  - **CP9.3**：run 进程 precheck 边界 ingest `console_inbox.jsonl`（读未消费行→InteractionIngest.inbound→
    Console.handle_inbound 分类落 directive/message；query→Mediator.handle_query 应答；游标持久化、幂等）。
    装配进 run.py。pause/resume/query 端到端测试。
  - **CP9.4**：真 Codex 跑 + console_server 并行 + 浏览器/HEADLESS 真查看实测 + README 控制台节 + 步⑨步级验证收口。
- 存量（非阻塞）：CP8.6b（eval target / import_defer / route dependency_wait）；运维执行/硬化（§7.4 T1/T2、双模 A/B、成本账网）。
- 已知边界：narrator 概念词典 + classify 的少量演示示例数值仅在**离线 demo 模式**出现（连线后 sendCmd 覆盖绕过
  classify），属 demo 回退、非自动渲染的真数据面；auto-render 面已全真化。
- 回退：`git revert 302261a`（纯前端 + 测试，无后端/DB 依赖，安全独立回退）。
