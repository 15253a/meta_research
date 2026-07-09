# 0040 · CP8.7 运维操作手册 + 步⑧步级验证收口（正式直接可用）

- date: 2026-07-09
- commit: d627a39 — docs: CP8.7 运维操作手册（README）+ 步⑧步级验证收口（M7）
- branch: main
- 检查点 / 步: CP8.7（属：步⑧ M7；正式可用性③——收口）

## 决策
用户 2026-07-09「最后要正式的系统直接可用的系统」的收口件：把 `meta-research/README.md`（原停在 M0 里程碑
说明）重写为**运维操作手册**——启动命令/参数、真 Codex 工程配置、goal_brief 写法、policy 旋钮、观测与
人工干预（status_card/console/文件请求）、停机语义、诚实边界、自验。这是「运维照着做就能把系统跑起来
并知道边界在哪」的门面。同时收口步⑧步级验证三条。

## 改动文件
- `meta-research/README.md` — 重写：M0 里程碑说明 → 运维操作手册（§0 一分钟跑起来 … §8 自验 + 目录布局）。

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内审（Opus 子代理）：REQUEST_CHANGES → 全修。逐条对照代码实证：命令/参数（run.py argparse）、环境
  变量（runner.py 逐字）、路径（status_card/transcripts/user_provided/research.sqlite）、goal_brief 契约
  （goalbrief.py）、policy 旋钮名+语义、停因串（advancer/stopcontroller/run.py）、研究形态（build+exec）、
  canary 边界、测试数 623、36 表——**绝大多数一致**。抓 1 BLOCKER + 2 SHOULD + 1 NIT 全修：
  ①[BLOCKER] §6 把 budget_exhausted 列为在用停因却漏「休眠」caveat（ledger 成本落账未接线，SUM 恒 0、
  永不触发——运维会误以为成本安全网护航）→ §6+§7 补休眠说明，对齐 stopcontroller.py/policy.yaml 既有
  诚实措辞；②[SHOULD] predicate_json 启动失败只首次建库成立（重启 DB goal 权威）→ §1 精确化；
  ③[SHOULD] --max-cycles 默认 100 → §0 补；④[NIT] 代理 env OS 级继承 → 注明。
- codex 第1轮：**APPROVE**（无 BLOCKER/SHOULD/NIT）。复核确认事实性问题全清；边界诚实（predicate 重启语义/max-cycles 默认/proxy OS 级/session_max 休眠/canary/eval·import·dependency_wait 后续/假执行历史语义均不 oversell）；运维可用性覆盖安装→自验→Codex env→入口→root→续跑→旋钮→观测→pause/resume→文件请求闭环→停因→硬边界，可交付。
- 第2轮：无需（第1轮即 APPROVE）。
- 未采纳意见及理由：无（内审 4 项全采纳；codex 零意见）。

## 验证
- **步⑧步级验证三条**（收口留证）：
  ① 全链 E2E：`pytest tests/test_run.py::test_full_attack_flow_end_to_end` → 通过（run.py 装配全系统跑通
     bootstrap→attack[真 gate 注册协议/占坑 + manifest 真子进程 smoke·train·eval + JudgeProvider 真评审
     落库 + 注册入池]→真证据关问→terminate）。
  ② 真 Codex CLI 冒烟：`python -m orchestrator.run --system-root . --work-root <dir> --max-cycles 6`
     exit 0——真 Codex 完整 attack build 链跑通（注册协议@1[自声明 2 metric]、占坑并训练 MLP baseline、
     真子进程 acc=0.9949、双评审 pass、legal 入池、τ score_floor 真实触发；build_log 0037 详载）。
     exec 已 mock E2E + kill-9 恢复证（build_log 0039），未单独跑真 Codex exec 冒烟（成本裁量）。
  ③ 冻结件锁：`pytest tests/test_frozen_contracts.py` → 通过（plan.schema sha256 + MIGRATION_SHA256 +
     无执行字段 语义锁，全程步⑧零漂移）。
- 全量：`pytest tests/ -q` → **623 passed**。
- 结论：通过。**步⑧「正式直接可用」达成**——一条命令让真 Codex 全自动跑完整研究元循环（build+exec
  target + 文件请求 + 自终止 + 崩溃恢复 + 人机控制），运维面文档化，边界诚实标注。

## 遗留 / 回退
- 遗留（诚实边界，README §7 已标）：①全局成本安全网休眠（ledger 成本落账未接，真长跑前须接）；②真 git
  worktree 隔离 + env lock 强校验（canary→硬化）；③CP8.6b = eval target（frozen schema create_evaluation
  缺 variant 引用需设计）+ import_defer/ImportWorker 装配 + route dependency_wait 特化；④§7.4 T1/T2 数百轮
  真跑 + 双模式 A/B 实测 = 运维执行。
- 回退：`git revert <HASH>`（纯文档）。
