# 0045 · CP9.4 人类控制台端到端验收 + README（步⑨收尾）

- date: 2026-07-09
- commit: cd97ee0 — test: CP9.4 人类控制台端到端验收 + README 控制台节（步⑨收尾）
- branch: main
- 检查点 / 步: CP9.4（属：步⑨ M8 人类控制台接入）——**本检查点收尾步⑨**

## 决策
**做了什么**：收尾步⑨。前三关（CP9.1 数据面 / CP9.2 前端真数据 / CP9.3 入站闭环）已把控制台接上真系统；
本检查点用**全栈端到端测试 + 实机验证 + 文档**把「系统在控制台上可查看/可交互」这一步级目标验收落地。

**为什么这么做**：步⑨的价值是「真接入、真查看、真交互」，必须有端到端证据（不是单元拼凑），且要为运维留
可操作文档。CP9.4 = e2e 测试（自动回归）+ 实机 curl（真 CLI 起服务留证）+ README 控制台节。

**影响面**：新增 1 e2e 测试 + README 一节。不改任何运行时代码（纯验收 + 文档）。

## 改动文件
- `meta-research/tests/test_console_e2e.py` — 新增（2 例）：起真 `console_server`（后台线程，port=0）→
  - ① 真视图：`GET /` 出真控制台页（含 adaptPayload/refreshDB 标记，非 mock 原型）+ `GET /api/db` 真数据
    （tables/status_card[snapshot_cycle=c1]/fs.roots）+ `GET /api/file` 白名单正例 + **逃逸负例拒 404**；
  - ③ 单写纪律【强证】：`_open_ro(mode=ro)` 连接写被 SQLite 物理拒（覆盖 console_server 全部 DB 访问）+
    DB **逻辑内容快照**（全表 dump 指纹，WAL-proof——不用主库 sha256，因文件库开 WAL 真写只落 -wal 会假绿）
    在所有 GET/POST 前后不变；
  - ② 入站闭环：`POST /api/message` 写 spool → `ConsoleInboxIngest` 消费 → pause directive →（确认消息**也经
    HTTP/spool/ingest**）`confirm_directive` → **确认 provenance 落库绑定**（payload.confirmation_message_id）→
    `precheck` 返回「pause 指令生效中」；`query` → **grounded 应答**（据卡渲染，含 快照 c1 / 当前问题 / q1）+
    重放 **no-dup**（删游标重 ingest，按 idempotency key 断 message 与 reply 各 1）。
- `meta-research/README.md` — 修改：§4.1「人类控制台（web 查看 + 交互）」——起 `console_server` 命令、浏览器
  查看、九标签页数据来源、入站闭环（pause/resume/query 分类 + 恰一语义）、单写纪律边界、未连线离线 demo 降级。

## Review（codex-chatgpt gpt-5.5/xhigh；两轮上限 §2.2）
- 外审第1轮：**REQUEST_CHANGES**。1 BLOCKER（零 DB 写用主库 sha256 假绿——基线在首 GET 后取 + WAL 下真写落
  -wal 主库字节不变）+ 3 SHOULD（确认绕过 ingest / query 未证 grounded / README 承诺无测试覆盖）+ 1 NIT——**全改**：
  改逻辑内容快照 + 基线前置、确认经 HTTP/spool/ingest、grounded 特征断言、/api/file 负例 + no-dup。
- 外审第2轮（=最后一轮）：**REQUEST_CHANGES**。1 BLOCKER（逻辑快照仍漏 no-op DML/sequence/DDL 等，不能"证明"
  零写）+ 3 SHOULD（provenance 未断言 / no-dup 假绿口 / 覆盖表述强）+ 1 NIT。**按 §2.2 第2轮后自行修毕、不再送审**：
  补**结构强证**（`_open_ro` mode=ro 写被物理拒——覆盖每个 GET/POST，逻辑快照降为补充）、断言 confirmation_message_id
  落库、no-dup 按 idem key 断 message+reply、grounded 加「当前问题」。
- 未采纳意见及理由：无（全采纳）。

## 验证
- 命令：`python -m pytest -q`
- 关键输出：
  ```
  664 passed in 123.07s
  ```
- **步级验证（步⑨「验证方法」①②③收口）**——本检查点收尾步⑨，逐条留证：
  - **① 真跑 + console 并行 → 看真实运行状态**：实机 CLI 起服务 `python -m orchestrator.console_server
    --system-root . --work-root <work> --port 8791` + curl（本机直连）：
    ```
    GET /            → <!doctype html>…（含 adaptPayload / refreshDB / /api/db 标记 = 真数据控制台页）
    GET /api/db      → keys: [fs, ledger_by_cycle, live, notification, policy, status_card, tables]
                       question 行数: 1 | snapshot_cycle: c1 | fs.roots: [work, schemas, prompts, policies, input]
    POST /api/message→ {"ok": true, "queued": {"connector":"console","raw_text":"暂停一下","seq":1,"idempotency_key":"console-1"}}
                       spool: {"connector":"console","raw_text":"暂停一下","seq":1,"idempotency_key":"console-1"}
    ```
    （真 Codex 端到端另见步⑧ build_log 0040 留证；本步聚焦「控制台真接入 + 真数据可视 + 入站闭环」。）
  - **② pause/resume/query 端到端**：test_console_e2e + test_console_ingest 覆盖 POST→spool→ingest→pause→确认→
    precheck 阻断→resume→解阻断、query→grounded 应答。绿。
  - **③ 测绿 + 单写纪律不破**：664 passed；e2e 强证 console_server 经 mode=ro 写被物理拒 + 逻辑快照不变（零 DB 写）。
- 结论：**通过。步⑨（M8 人类控制台接入）达成**——系统在人类控制台上可查看真实状态、可经命令行交互（pause/
  resume/query），单写纪律不破。

## 遗留 / 回退
- **步⑨完成**。剩余存量（非阻塞，另行排期）：
  - CP8.6b：eval target（免训练评估）+ import（外部基线导入）+ route dependency_wait 特化。
  - 运维硬化（§7）：成本记账接线（`INSERT INTO ledger` → budget.session_max 安全网转活）、git worktree 隔离 +
    env lock 强校验、双模 A/B 会话粒度实测。
  - CP9.3 已知取舍：`_attempts` 不跨重启持久化（注释在案）。
- 回退：`git revert cd97ee0`（纯测试 + 文档，无运行时耦合，安全）。
