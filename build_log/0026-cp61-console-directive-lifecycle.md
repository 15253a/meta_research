# 0026 · CP6.1 保守分类器 + directive 生命周期（M5 首检查点）

- date: 2026-07-08
- commit: 702071e — feat: CP6.1 保守分类器 + directive 生命周期（M5）
- branch: main
- 检查点 / 步: CP6.1（属：步⑥ M5 人类控制台 + query 只读应答器）

## 决策
M5 开工。落人机窗口的**入站三件套**：确定性保守分类 → directive 生命周期（润色≠raw + 回显确认）→
按时机消费（效果 + DECISION + 状态迁移单事务）。

- **KeywordClassifier**（`console.py`）：廉价关键词确定性分类，对齐 DDL 四意图
  （query/directive/note/unclear）。保守铁律：指令词表先查（宁误报 directive——有确认门兜底）；
  query 只认**实义状态词**（裸疑问助词 吗/？故意不收——礼貌式指令"停掉好吗"须进澄清环而非被静默
  只读作答）；词表未命中一律 unclear 不猜。ASCII 词要求词边界（防 "pin"∈"opinion" 软指令假阳自动
  消费污染台账）。真语义润色/分类 = M6，本版确定性可回放 stopgap。
- **Console.handle_inbound**：durable 幂等入站（复用 M1c InteractionIngest）→ 恰一分类
  （UNIQUE(message_id)；幂等重放返回与首次**等价**——含 needs_confirmation，经
  _existing_classification 回读 directive 态补齐；并发重放撞 UNIQUE 则捕获 IntegrityError 回读既有）
  → **同事务先插 directive(pending) 再插分类行**（trg_iclass_directive_prov 要求 directive 先存在且
  source 回指同 message）。note 是独立 intent：分类行 directive_id 必空（CHECK），
  directive(kind=note,soft) 仍建、provenance 走 source 回指，重放经 source 找回。
- **确认门**：硬指令回显确认 = payload.confirmed 标志（DDL status 无 confirmed 态）；未确认硬指令
  consume 一律拒（§7.1 M5 验收行）。reject 双路：用户否润色稿 → rejected + 理由入
  payload.rejection_reason（审计/通知用），不写 decision（P1）；系统不从软指令 →
  DECISION(soft_directive_declined, 理由) + rejected（硬指令禁走此路）。
- **consume_directive**：**单事务内**读校验（防 TOCTOU）+ 最小效果 + DECISION(actor='human',
  directive_id 回指) + 条件更新 consumed（rowcount 兜底；confirm/reject 同型防护）。
  **pause/resume 状态模型（外审第 1 轮 BLOCKER 后重定义）**：pause 消费 = 进入暂停态（该 DECISION
  即记账）；阻断谓词 has_blocking_pause = 最近被消费的 pause/resume 是 pause（按 consumed_decision_id
  消费序）；resume 消费解除阻断并把**早于它的** pending pause 置 superseded；pending（含已确认）
  不阻断——Advancer 前置检查顺序 = 先消费到期 directive 再查阻断（CP6.3 接线）。其余效果：
  abort_cycle（唯一非终态轮 aborted；单轮在途约定）/inject_question（source='human' status='open'；
  goal_id=1+MAX(version) 单目标约定同 statestore）/prune_branch（decision(type=prune_branch) 先行再
  dead_end——trg_q_deadend 时序；**该决策即消费决策**，一次消费恰一条人类决策）/
  set_budget·reprioritize·goal_amend·note = 记账消费（真效果接线=M6，代码注明）。
  pending_directives 只出「软或已确认硬」（未确认硬不进消费队列，提醒归通知层 CP6.3）。
- statestore_sqlite.consume_directive 桩消息改指 console（职责归口）。

## 改动文件
- `meta-research/orchestrator/console.py` — 新增：KeywordClassifier + Console（入站/确认/拒绝/消费/
  pending_directives/has_blocking_pause）+ sanitize + _hit 词边界。
- `meta-research/tests/test_console.py` — 新增：18 测（分类矩阵/礼貌式指令回归/词边界回归/sanitize/
  unclear 负例/润色≠raw+provenance/幂等重放等价/未确认硬拒→确认后消费+DECISION/用户拒理由入账不写
  decision/软不从记理由/inject/prune+deadend 时序+单决策/pause 消费序阻断全时序/resume 只清早先
  pause/待消费队列过滤/abort/note 分类行 NULL/二次消费拒）。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图加 console.py 行。
- `meta-research/orchestrator/statestore_sqlite.py` — 修改：consume_directive 桩 NotImplementedError
  消息改指 console 归口。
- `ROADMAP.md` — 记账：M5 状态→进行中 + CP6.1–6.3 检查点计划登记。
- `implement_note.md` — 记账：现场快照随检查点入库。

## Review
- 内审（Opus 子代理）：APPROVE 无 BLOCKER。2 SHOULD 已修：①裸疑问助词不当 query 证据（"停掉好吗"
  漏进只读作答→改 unclear 澄清环）；②ASCII 词界（"pin"∈"opinion" 软指令假阳）。NIT 已按注释/删除处理。
- codex（gpt-5.5/xhigh）第1轮：REQUEST_CHANGES——2 BLOCKER + 4 SHOULD，全部采纳修复：
  ①[BLOCKER] consume_directive 事务外读→TOCTOU：读校验入同事务+条件更新 rowcount 兜底（confirm/
  reject 同型一并修）；②[BLOCKER] pause 生命周期自相矛盾（消费即失阻断）：重定义状态模型（消费=进
  暂停态，阻断按消费序判定，resume 消费解除）；③[SHOULD] 分类预检事务外并发重放撞 UNIQUE：捕获
  IntegrityError 回读既有（幂等）；④[SHOULD] resume 误清晚到 pause：限定 id< 本 resume；⑤[SHOULD]
  prune_branch 双决策：prune 决策即消费决策；⑥[SHOULD] pending_directives 出未确认硬指令：过滤。
  各配回归测试。
- codex 第2轮：**APPROVE**（确认第 1 轮主问题全部对齐）。随附 1 SHOULD + 2 NIT 亦已修：
  重放返回补 needs_confirmation（确认 UI 不因 connector 重放丢失）；confirm/reject 补 rowcount 兜底
  （风格与 consume 一致）；用户拒绝理由持久化入 payload。
- 未采纳意见：无。

## 验证
- 命令：`python -m pytest tests/test_console.py -q` → **18 passed**；`python -m pytest tests/ -q` →
  **454 passed**（436 基线 + 18 新，无回归）。
- 结论：通过。

## 遗留 / 回退
- 待办（M5 后续）：CP6.2 query 只读应答器（authorizer 写拒负例/grounding+模板回退/status_card
  latest_decision 接线/中介重建/SLA）；CP6.3 通知矩阵 outbox + 文件请求全流水 + 全局等待 +
  Advancer 接「先消费到期 directive → 再查 has_blocking_pause」前置检查。M6：真 Codex 润色与语义
  分类、set_budget/reprioritize/goal_amend 真效果接线、prune_branch 子树级联语义、多轮并发 abort。
- 回退：`git revert 702071e`（console.py 独立新模块，回退不波及既有链路）。
