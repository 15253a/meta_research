# 0042 · CP9.1 人类控制台数据面（console_server）

- date: 2026-07-09
- commit: 295f84d — feat: CP9.1 人类控制台数据面（console_server）——步⑨ M8 开工
- branch: main
- 检查点 / 步: CP9.1（属：步⑨ M8 人类控制台接入）

## 决策
用户 2026-07-09 指出「人类控制台原型没有接入系统」，最终目的是系统能在人类控制台上查看——**纠正 CP8.8 期
「系统无 web 组件」的结论**。步⑨ = 把原型 `reference/人类控制台原型-v2.html` 接上真系统。本检查点 = 数据面：
`orchestrator/console_server.py`（独立进程 HTTP 服务，把真运行库投影成控制台前端消费形状 JSON + 收人工入站）。

**单写纪律铁律不破（§6.6）**（最高裁量）：控制台是独立进程，对研究库**只读**（mode=ro 物理只读，写必失败），
入站只**追加写 <work>/state/console_inbox.jsonl**（run 进程 precheck 边界 ingest = CP9.3），**绝不写 DB**。
零新依赖（stdlib http.server）。动态列投影（PRAGMA 取真列名，DDL 冻结则形状稳），不硬编码列名。

## 改动文件
- `meta-research/orchestrator/console_server.py` — 新增：/api/db（真表动态列投影 + 派生 status_card/live/
  notification/policy/ledger_by_cycle/FS）+ /api/file（白名单虚拟根映射 + resolve containment）+ /api/message
  （spool 写，connector 固定 console）+ 静态服务；错误泛化报 + traceback 落 stderr。
- `meta-research/tests/test_console_server.py` — 新增：13 测。
- `ROADMAP.md` — 步⑨登记（方案 + CP9.1–9.4 切分）+ CP9.1 勾选。

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内审（Opus 子代理）：APPROVE。实证：mode=ro 拒写（单写纪律硬保证）、路径逃逸/symlink 闭合、6 测真起
  服务真请求。采纳 3 SHOULD（enqueue 并发 seq 锁串行 + 坏 policy 降级 + heartbeat_age_s 单义）+ 2 NIT。
- codex 第1轮：REQUEST_CHANGES——1 BLOCKER + 5 SHOULD/NIT，全部处理：
  ①[BLOCKER] 本 diff 夹带回滚 CP8.8 no-wedge 修复——**根因=环境污染**（session-start 工作区已有未提交的
  CP8.8 反向改动，我的 git add -A 误 staged）→ `git checkout HEAD --` 把 attack_stages/compiler_sqlite/
  statestore_sqlite/interfaces/advancer/test_attack_advance + build_log/0041 全部还原到 CP8.8 态（
  persist_selection_safe 与其回归测试完好、全绿），本提交 staged diff 仅含 console + ROADMAP（已核实）；
  ②[SHOULD] _serve_static symlink → resolve+containment；③[SHOULD] read_file base.parent 脆 → 虚拟根显式
  映射（work_root 任意名可用）；④[SHOULD] _notifications 撕裂尾行 → 无 \n 尾行丢；⑤[SHOULD] /api/db 错误
  泄敏感 → 泛化报；⑥[NIT] connector 固定 console。各配回归。
- codex 第2轮：**APPROVE**（CP8.8 还原确认、路径闭合、单写纪律无 DB 写路径；附 NIT：500 承诺日志须真落 →
  traceback.print_exc 已加）。
- 未采纳意见及理由：无（全部采纳）。

## 验证
- 命令：`python -m pytest tests/test_console_server.py -q` → 13 passed；`python -m pytest tests/ -q` →
  **638 passed**（CP8.8 基线 625 + console 13，无回归）。
- 单写纪律实证：test_assemble_db_no_db_write（mode=ro INSERT 抛 + 组装前后 DB 行数不变）；
  test_enqueue_message_spool_only（interaction_message 未增，只写 spool）。
- 路径安全实证：../逃逸拒 / symlink 越界拒 / 虚拟根任意 work 名命中 / 非白名单根拒。
- HTTP 端到端：真起 ThreadingHTTPServer（port=0）+ 真请求 /api/db·/api/file·/api/message（proxy 绕过）。
- 结论：通过。

## 遗留 / 回退
- 待办：CP9.2 前端接入（views/console/index.html 由原型派生换数据源）；CP9.3 入站闭环（run 进程 precheck
  边界 ingest spool 走 M5 链）；CP9.4 端到端真查看验收 + README 控制台节。
- **教训**：新 session 开工前须核 `git status`——工作区若带未提交改动（本次的 CP8.8 反向改动），`git add -A`
  会误 staged。已在本日志留证；后续开工先看 git status。
- 回退：`git revert 295f84d`（console_server + 测试纯新增，不影响 638 基线的其余）。
