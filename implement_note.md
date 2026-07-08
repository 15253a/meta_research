# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 ｜ 位置：步⑥（M5）CP6.1
- 检查点状态：外审第 2 轮进行中（codex 后台任务 bzyh7348k；出结论后按 §2.2 走 commit）

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**CP6.1 保守分类器 + directive 生命周期**：`orchestrator/console.py`——KeywordClassifier（确定性保守
分类，词表未命中即 unclear；裸疑问助词不当 query 证据；ASCII 词界）+ Console（幂等入站→恰一分类→
directive-pending 先行[trg_iclass_directive_prov 时序]；回显确认门；消费单事务）。
**内审（Opus）过**：APPROVE，2 SHOULD（query 助词/词界）已修。
**外审第 1 轮 REQUEST_CHANGES，6 条全修**：①consume TOCTOU→读校验入事务+条件更新 rowcount 兜底
（confirm/reject 同修）；②pause 生命周期矛盾→**新状态模型：pause 消费=进暂停态，阻断=最近被消费的
pause/resume 是 pause（按 consumed_decision_id 序），resume 消费解除；pending 不阻断（Advancer 先
消费后查阻断，CP6.3 接线）**；③分类并发重放撞 UNIQUE→捕获 IntegrityError 回读既有；④resume 只清
早于它的 pending pause；⑤prune_branch 一次消费恰一条决策（prune 决策即消费决策）；⑥pending_directives
过滤未确认硬指令。测试 18 个（+4 外审回归），**全套 454 绿**。

## 工作区状态
- 已 staged 全部：console.py / test_console.py / __init__.py / statestore_sqlite.py / ROADMAP.md /
  implement_note.md。r2 diff 已导出 scratchpad/cp61_diff_r2.txt。
- **外审第 2 轮后台跑着**（bzyh7348k → scratchpad/cp61_review_out{_r2}.md）。

## 下一步动作（按序）
1. 读 cp61_review_r2_out.md：APPROVE → commit；仍有 BLOCKER → 按 §2.2 自行裁决修改、不再送审、commit。
2. build_log 0026（草稿在 scratchpad/build_log_0026_draft.md，补 review/hash）+ INDEX + 勾 ROADMAP
   + 刷新本文件 → docs commit。
3. 接 CP6.2：query 只读应答器（authorizer 写拒负例+grounding+模板回退+status_card latest_decision
   接线[cycle scope]+中介重建一致+SLA）。设计要点已勘：gate_sqlite.open_gate_read_conn 是 mode=ro+
   authorizer 现成范式；selection DECISION 尚无（persist_selection 只写 cycle.next_*）——latest_decision
   接线方案届时定（选项：advance 落 selection DECISION 或按 cycle_id 查最近 decision 摘要）。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；APPROVE 才 commit；**外审 diff 排除记账类**（`':(exclude)build_log/**' ':(exclude)implement_note.md'`）。
- **CP6.1 关键 DDL 事实**（已核）：directive 表无触发器（状态迁移代码强制）；interaction_classification
  UNIQUE(message_id)+CHECK(intent='directive' ⇔ directive_id 非空)+trg_iclass_directive_prov 要求 directive
  先存在且回指同一 message → **必须先插 directive(pending) 再插分类行**；note 意图也建 directive(kind=note,soft)
  但分类行 directive_id=NULL（provenance 走 directive.source_interaction_message_id）；question.source 含
  'human'；trg_q_deadend 要求 prune_branch decision 先行。
- **评审极能抓真 bug**：M4 期两层评审共抓 ~9 BLOCKER。M5 涉人机窗口：重点自查分类幂等重放、确认与消费竞态、
  pause 阻断语义（未确认不阻断）、consume 单事务完整性。
- gate/文件库测试须**文件库**（authorizer 读连接独立）；statestore/console 可 :memory:。
- DDL 字节冻结：改 schema 须同步 database.py MIGRATION_SHA256（走评审）。policy.yaml/schema 改动=决策性。
- 测试基线现 **449**。
- 悬案/M6 硬化清单（build_log 0023–0025 遗留汇总）：注册段合一事务、bundle pc 产物集哈希锚、route plan
  后特化（eval_only/reuse_only/dependency_wait）、attack/import 注册骨架共享、完整供应链 manifest、修复重评
  轮数、report 制品、materialize_failed 重试（按 selection_key 取下一候选）、resolve_deps dead_end 悬案、
  compiler 检索区接 recall、**console 效果接线**（set_budget runtime override/reprioritize score 权重/
  goal_amend 版本升级/真 Codex 润色）。
