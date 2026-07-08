# 0031 · CP7.3 run.py 全系统装配入口（M6）

- date: 2026-07-08
- commit: ce11d00 — feat: CP7.3 run.py 全系统装配入口（M6）
- branch: main
- 检查点 / 步: CP7.3（属：步⑦ M6 长跑 + 验收剧本）

## 决策
**「系统完整运行、进入全自动」的落点**：一条命令把真组件（冻结 DDL 库/单写 WriteDaemon/真状态机/真编译器/
真发布器/StopController/人机 precheck）+ StageProvider(真 CodexRunner) 接成全自动元循环，驱动 run_cycles
到停机。**已真 Codex CLI 冒烟**：`python -m orchestrator.run` 真跑一轮——Codex 建真根问题、cycle done
route=bootstrap next=decompose、决策落账、status_card 发布。

- **build_system**：装配序幂等可恢复——goal_brief 解析 → database.connect（新库建/既有续，checksum 三重
  锁）→ WriteDaemon → SQLiteStateStore（首次建 goal，`SELECT 1 FROM goal` guard 幂等）→ **goal_body_md
  取 DB goal.text 权威**（重启若 operator 改 brief，用新 brief 会与 DB goal_ver 漂移污染 context pack
  +卡片摘要、绕过 goal_amend）→ SqliteCompiler/StatusPublisher（独立连接同文件库 WAL）→ StopController+
  Console+make_advancer_precheck → StageProvider(CodexRunner) → SqliteAdvancer(全注入)。
- **System.run**：run_cycles 到停机（terminate/τ 自终止/阻断/max_cycles）。
- **main CLI**：--system-root/--work-root/--max-cycles；捕获 attack NotImplementedError 转干净报（exit 2，
  非裸 traceback）；停因 print 精确化。
- **范围**：reasoning-only 全自动闭环。attack=None（judge/idea/plan provider=CP7.4）。双模式 A/B：
  reasoning-only 下 A≡B（每轮一阶段=一 turn），真 A/B 分驱随 attack 落 CP7.4。

## 改动文件
- `meta-research/orchestrator/run.py` — 新增：build_system（全装配）+ System（run/last_stop_reason）+ main CLI。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图加 run.py。
- `meta-research/tests/test_run.py` — 新增：7 测（全装配+terminate[goal/cycle/question/card/db 落盘]/
  重启不重建 goal/durable 停机端到端/τ score_floor 端到端自停/goal_body 取 DB 非篡改 brief/main CLI 冒烟/
  全局等待端到端阻断）。

## Review
- 内审（Opus 子代理）：REQUEST_CHANGES → 2 SHOULD 全修：①goal_body_md 重启漂移（改 brief 后编译器/发布器
  用新 brief 与 DB goal_ver 漂移、污染 context pack「目标全文」+卡片、绕 goal_amend）→ 取 DB goal.text
  权威 + 回归；②τ score_floor 无经 run.py/run_cycles 轮后 check_after_round 端到端测试 → 加 e2e。NIT 处理：
  main 捕获 attack NotImplementedError 干净报 + 停因 print 精确 + main() argparse 冒烟测试。三连接安全性/
  幂等 resume/attack=None 干净失败 逐项实证通过。
- codex（gpt-5.5/xhigh）第1轮：REQUEST_CHANGES——1 BLOCKER + 2 SHOULD + 1 NIT，全部采纳修复：
  ①[BLOCKER] compiler 注入普通可写连接破坏单写纪律 → 改 open_responder_read_conn 只读连接（入口层
  enforce 只读边界）；②[SHOULD] 恢复路径无条件 parse_goal_brief（缺失/畸形 brief 卡死可续既有库）→
  仅首次建 goal 才解析 brief；③[SHOULD] CLI 停因误报（阻断被 idle 掩盖）→ 停因优先级 τ>阻断>收尾+回归；
  ④[NIT] 无 exit-2 干净报测试 → 补 test_main_cli_attack_clean_error。
- codex 第2轮：**APPROVE**（确认 4 点全闭环：只读连接单写边界成立、恢复只在无 goal 时解析 brief +
  goal_body 取 DB 权威、停因优先级覆盖阻断、exit-2 干净报有覆盖）。随附 1 NIT：docstring 装配序仍写旧序
  → 已改为「connect→查 goal→仅首次 parse brief/create_goal」。
- 未采纳意见及理由（如有）：无。

## 验证
- 命令：`python -m pytest tests/test_run.py -q` → **9 passed**；`python -m pytest tests/ -q` → **524 passed**
  （CP7.2 后基线 515 + 9 新，无回归）。
- **真 Codex 冒烟**：`python -m orchestrator.run --system-root . --work-root <tmp> --max-cycles 1` → exit 0，
  「推进 1 轮 c1 停因=provider terminate/max_cycles」；DB：goal 1、cycle1 done route=bootstrap next=decompose、
  Codex 建的真根问题、decision(goal_bootstrap+create_root)、status_card.json 发布。
- 结论：通过（reasoning-only 全自动闭环真 Codex 端到端跑通）。

## 遗留 / 回退
- 待办 CP7.4：judge provider + idea/plan 接真（CP7.2 前置①idea_set schema↔attack_stages content_md 校准、
  ②sidecar→create_file_request 桥）+ §7.3 机制验收剧本集成测试（attack 全链）+ 真 stage-granular 双模式
  A/B 分驱 + M6 步级验证收尾。
- 回退：`git revert ce11d00`（run.py 独立新入口，回退不波及既有组件）。
