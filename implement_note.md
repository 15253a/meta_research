# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 ｜ 位置：步⑥（M5）CP6.3
- 检查点状态：自验通过（482 绿=470+12；M5 步级联合勾兑 57/57）→ 内审/外审中。
  改动=notify.py 新建（Outbox 文件队列幂等/DirectiveNotifier 7 态扫描派生/FileRequestService
  create_checked·resolve·cancel/FileRequestNotifier 3 事件[now 注入]/make_advancer_precheck 全局等待）
  + console.py consume cycle_id 可空 + advancer.py precheck 装配（run_cycles 开轮前+格间查阻断）
  + tests/test_notify.py(12)。

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**CP6.3 目标**（ROADMAP 步⑥第三检查点，收尾 M5）：
1. **outbox 幂等投递**（实现层文件队列，DDL 冻结不建表）：outbox.jsonl 追加 + delivered 标记；
   event_key 幂等去重；Connector.send 投递（M5 测试用 Fake connector 收集）。
2. **通知矩阵**：directive 逐态推送（received/classified/pending_confirmation[展示润色稿]/
   pending_effect[预计消费点]/applied[consumed_cycle+效果摘要]/rejected[附理由]/superseded 7 态）+
   文件请求 3 事件（request_pending/reminder[remind_interval_h，注入 now 保确定性]/resolved）。
   事件从 DB 状态**扫描派生**（event_key = directive:{id}:{state} 幂等），非 console 内联发——
   console 不知 outbox，保持单一职责。
3. **文件请求全流水**：create_checked（schema 校验[resource_request.schema.json，缺 attempted_paths/
   failure_reason 拒] + policy 拒绝三判据[enabled=false / len(items)>max_items_per_request=10 /
   同 goal (pending+resolved)≥max_requests_per_goal=5]）→ resolve（uploads/<req>/<item_no>/ →
   sha256 → 复制并入 input/user_provided/<req>/ → resolution_json+resolved_message_id 一次性迁终态
   [trg_ireq_identity_frozen 只许这一跳]）/ cancel 同 provenance。
4. **全局等待 + Advancer 前置检查**：precheck 可选装配（同 status_publisher 模式）——每格 advance 前：
   先消费到期 directive（immediate/stage_boundary，经 console），再查 has_blocking_pause() OR 存在
   pending interaction_request → 阻断（run_cycles 停止推进返回，query/通知照常）。consume_directive
   需允许 cycle_id=None（阻断检查可发生在无在途轮时；DECISION.cycle_id 本可空）——console.py 小改，
   随本检查点评审。
5. **M5 步级验证**：§7.1 M5 行逐项跑（build_log 0028 附证据）。

## 工作区状态
- 干净（28c6117 + 本 docs 提交后）。测试基线 **470 绿**。
- scratchpad：M5_refsheet.md / cp61_* cp62_* 评审材料 / build_log_0027_draft.md（已誊入库）。

## 下一步动作（按序）—— CP6.3
1. 新模块 orchestrator/notify.py（Outbox + DirectiveNotifier 扫描派生 + FileRequestService
   create_checked/resolve/cancel）；console.py 允许 consume cycle_id=None；advancer.py 增 precheck
   可选装配（研究阻断，run_cycles 每格前查）。
2. 测试：7 态逐态推送断言 / outbox 幂等（重扫不重发）/ 文件请求创建三负例+schema 拒 / uploads
   hash 入账并入+恢复推进 / 全局等待（pending 请求→run_cycles 不推进而 query/通知照常）。
3. §5 循环：内审 Opus → codex ≤2 轮（模式 B 后台）→ commit → build_log 0028 + M5 步级验证证据。
4. M5 收尾后进入步⑦（M6）：长跑 + §7.3/§7.4 验收剧本 + OPEN #4 确认 + 硬化清单。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；APPROVE 才 commit；**外审 diff 排除记账类**（`':(exclude)build_log/**' ':(exclude)implement_note.md'`）。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- **M5 已定语义**（CP6.1/6.2 评审沉淀，别改回去）：pause 阻断=最近被消费的 pause/resume 是 pause
  （consumed_decision_id 消费序；pending 不阻断→前置检查=先消费再查）；pending_directives 只出
  「软或已确认硬」；查询文本按 message_id 从持久层取；实体 grounding 环视+大小写不敏感；temp schema
  在 mode=ro 下仍可写（authorizer 拒 TEMP*/VTABLE）；rebuild 相关子查询防扇出。
- gate/mediator/发布器测试须**文件库**；statestore/console 可 :memory:。
- DDL 字节冻结：outbox/状态卡都是实现层文件，**勿建表**。policy.yaml/schema 改动=决策性。
- 测试基线现 **470**。
- 悬案/M6 硬化清单：注册段合一事务、bundle pc 产物集哈希锚、route plan 后特化、attack/import 注册
  骨架共享、完整供应链 manifest、修复重评轮数、report 制品、materialize_failed 重试、resolve_deps
  dead_end 悬案、compiler 检索区接 recall、console 效果接线（set_budget/reprioritize/goal_amend 真
  效果+真 Codex 润色/语义分类）、prune_branch 子树级联、多轮并发 abort、真 Codex 应答器（kind=codex+
  runner_call 绑定）、发布失败重试、p95 压测口径。
