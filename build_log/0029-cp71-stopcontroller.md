# 0029 · CP7.1 长跑自终止安全网（§4.4.6 τ 三判据强制）

- date: 2026-07-08
- commit: da38b2f — feat: CP7.1 长跑自终止安全网（§4.4.6 τ；M6）
- branch: main
- 检查点 / 步: CP7.1（属：步⑦ M6 长跑 + 验收剧本）

## 决策
M6 开工。系统目标=「完整运行、进入全自动」。本检查点补齐**长跑自终止安全网**——run_cycles 原本只在
reasoning provider 返回 next_intent='terminate' 时停机（=τ判据③，Codex 判）；「数百轮无人值守不漂移」
要求编排器**自己**能停，防 provider 永不 terminate 无限空转。

- **StopController（新）** §4.4.6 τ：
  - 判据① 价值衰退：open/active 前沿最高分连续 N 轮 < score_floor（policy.flow.tau）。**只在前沿全部
    已评分时**判定衰退（任一 NULL 分=未评估≠低于 τ → 非衰退，严格保守防混合前沿误停）；作用域含
    open+active（对齐 ROADMAP「open/active 最高分」）；连续计数进程内、每轮完成后 tick 一次；恢复重启
    计数归零=保守（只延后不误停），真触发即落 durable global_stop、重启拒推进。
  - 判据② 预算耗尽：全局成本台账 ledger.money 求和 ≥ policy.budget.session_max（ledger 覆盖所有 phase
    含 reasoning/Codex，全局成本唯一权威源）。**ledger 写入=M6 硬化未接线 → SUM=0、本网休眠、待成本
    落账即生效**。session_max=null 关闭。
  - 判据③ 谓词满足：仍由 provider terminate（研究判断，不在本控制器）。
  - 停机 durable：DECISION(actor='orchestrator', type='global_stop', reason)；already_stopped 检存在→
    run_cycles 启动即拒推进（进程内计数丢失也不影响：停机事实已落库）。
- **advancer.run_cycles**：开循环前 already_stopped 拒推进；每轮 ids.append 后 check_after_round（命中→
  durable global_stop + break）。stop_controller=None 行为不变。
- **policy/schema**：policy.yaml budget 加 session_max: 100000（判据②上限，失控兜底、A/B 后细化，null
  关闭）；schema 加 session_max: ["number","null"]。**非 DB DDL（冻结锁不受影响）**；compute_budget 只读
  B0/B_max 向后兼容；spotcheck 同步。
- **OPEN #4 裁决**（M6 阻塞项，全自动自主裁决）：paper-gap **不引入独立谓词机制**——「论文可写/缺口
  闭合」已被 goal.predicate_json 成功条件涵盖（满足目标谓词即 paper-gap 闭合，由既有 reasoning 收口 I3
  + τ判据③ 判定）；另设审计题会与 τ判据③重复且破坏 §2.3 七形态封闭。裁决落 ROADMAP + 本 log。
- **M6 建造/执行边界裁决**（全自动）：M6 建造面=让系统「完整运行进入全自动」的机器（自终止安全网 +
  真 Codex 装配入口 + 双模式 + §7.3 机制集成验收）；§7.4 T1/T2 是真算力多日运维执行（数百轮×24h 真
  EEG），非本轮建造——建造交付=系统可全自动启动 T1/T2 且机制验收过，跑到科学结论属运维。

## 改动文件
- `meta-research/orchestrator/stopcontroller.py` — 新增：StopController（already_stopped/record_stop/
  _budget_exhausted/_score_floor_hit/check_after_round）。
- `meta-research/orchestrator/advancer.py` — 修改：__init__ 增 stop_controller + last_stop_reason；
  run_cycles 接线（启动 already_stopped + 每轮 check_after_round）。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图加 stopcontroller.py。
- `meta-research/policies/policy.yaml` — 修改（决策性）：budget.session_max: 100000。
- `meta-research/schemas/policy.schema.json` — 修改（决策性）：budget.session_max ["number","null"]。
- `meta-research/tests/test_stopcontroller.py` — 新增：12 测（预算停+null 关闭/分数连续停+回升重置/全
  NULL 不衰退/NULL 混合不衰退[BLOCKER 回归]/含 active 作用域[SHOULD 回归]/无前沿不衰退/record_stop 幂等/
  已停拒推进/跑中自停/provider terminate 共存[NIT 回归]）。
- `meta-research/tests/test_schemas.py` — 修改：budget spotcheck 加 session_max。
- `ROADMAP.md` — M6 计划注册 + OPEN #4 裁决 + 建造/执行边界（含决策，进外审）。

## Review
- 内审（Opus 子代理）：REQUEST_CHANGES → 全修：[BLOCKER] 判据① NULL 混合前沿误停（一低分+一未评→
  MAX 排 NULL=0.1<floor 误触发，durable 不可恢复地终止活跃研究）→ 前沿全评分才判衰退+双向回归；
  [SHOULD] 作用域漏 active（ROADMAP 指定 open/active）→ 含 active+回归；[SHOULD] 预算源误用
  run.cost+attempt.cost（分执行局部、漏 reasoning/Codex）→ 改指全局台账 ledger.money + 休眠状态如实
  注明；[NIT] 无 provider-terminate 共存回归 → 补。其余（短路良性/tick 语义/durable 幂等/schema 非
  DDL/policy 向后兼容）逐项实证通过。
- codex（gpt-5.5/xhigh）第1轮：REQUEST_CHANGES——1 BLOCKER + 2 SHOULD + 1 NIT，全部采纳修复：
  ①[BLOCKER] 预算耗尽不恢复安全（只轮后评估，重启若 ledger 已超限但崩在落 global_stop 前会先白跑一
  整轮）→ 拆出纯读 check_before_round，run_cycles 每轮开工前（含重启后首轮）先评预算 + 回归（provider
  一次未调证不白跑）；②[SHOULD] ROADMAP 文本仍写「run/attempt cost 求和」→ 改「ledger.money」；
  ③[SHOULD] record_stop check-then-insert 非原子 → 检查与插入同事务 + 单活 orchestrator 假设注明；
  ④[NIT] top_open_score 字段名与 open+active 范围不符 → top_frontier_score。OPEN #4 复核认同自洽，
  按其提醒补「机器契约」说明（goalbrief.py 强制 predicate_json，非仅文档）。
- codex 第2轮：**APPROVE**（确认预算门恢复安全已实质修复、provider 未被调用有断言；record_stop 事务内
  幂等 + 单活假设讲清；分数衰退对 NULL 前沿/open+active/durable 自洽）。随附 2 NIT 亦已修：
  check_before_round 措辞明写「命中会 record_stop」（非纯无副作用）；record_stop 的 payload 改
  {**detail, reason} 序（入参 reason 权威、防未来 detail.reason 覆盖）。
- 未采纳意见及理由（如有）：无。

## 验证
- 命令：`python -m pytest tests/test_stopcontroller.py -q` → **13 passed**；`python -m pytest tests/ -q`
  → **502 passed**（M5 后基线 489 + 13 新，无回归）。
- 结论：通过。

## 遗留 / 回退
- 待办（M6 续）：CP7.2 真 Codex 生产装配（StageProvider 适配器+全系统入口 run.py+kill-9 真栈恢复冒烟）；
  CP7.3 会话双模式 A/B（policy.session.mode_default）；CP7.4 §7.3 机制验收剧本集成测试 + M6 步级验证。
  硬化：ledger 写入（成本记账，判据②落账即生效）、cycle.cost_total 维护。
- 执行交付（本 session 外）：§7.4 T1/T2 真跑。
- 回退：`git revert da38b2f`（stopcontroller 新模块 + advancer/policy/schema 小增量，回退不波及 M0–M5）。
