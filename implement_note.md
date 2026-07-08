# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 ｜ 位置：步⑥（M5）CP6.2 开工
- 检查点状态：构建中（CP6.1 已完成 702071e + docs 记账；CP6.2 = query 只读应答器 + 中介 + status_card 接线）

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**CP6.1 已完成**（702071e，build_log 0026）：console.py 保守分类器 + directive 生命周期。要点：pause
状态模型 = 消费即进暂停态、阻断谓词 has_blocking_pause 按 consumed_decision_id 消费序（最近消费的
pause/resume 是 pause）、resume 消费解除并只清早于它的 pending pause；pending（含已确认）不阻断——
**Advancer 前置检查顺序 = 先消费到期 directive 再查阻断（CP6.3 接线）**。pending_directives 只出
「软或已确认硬」。测试基线 **454 绿**。

**CP6.2 目标**（ROADMAP 步⑥第二检查点）：query 只读应答器 + 中介 + status_card 接线——
- responder 只读边界：mode=ro URI + authorizer 写拒（负例：写 decision/route/question/cycle/
  metric_result 全被拒）；范式照 gate_sqlite.open_gate_read_conn（mode=ro 物理只读 + authorizer）。
- grounding 校验（不得声称卡外事实/状态已变/引 log 作证/自产 directive）不过 → 模板回退。
- status_card 发布接线：advance 阶段边界原子发布（tmp→rename）+ latest_decision 按 cycle scope 补
  （注意：persist_selection 只写 cycle.next_*、无 selection DECISION——方案二选一：advance 落
  selection DECISION，或按 cycle_id 查最近 decision 摘要；届时定并写入受审制品）。
- 中介线程重建：interaction_* + 最近发布 status_card 重建（mediator_rebuild_last_n=20），前后回答
  一致断言。
- ACK/query p95<2s 断言（确定性路径可测）。

## 工作区状态
- 干净（CP6.1 检查点提交 702071e + docs 提交已落）。
- scratchpad：M5_refsheet.md（M5 契约摘要）、cp61_* 评审材料、build_log_0026_draft.md（已誊入库）。

## 下一步动作（按序）—— CP6.2
1. 精读 interfaces.py Responder/StatusPublisher/Connector Protocol + interaction.py ack/create_file_request
   + policy.interaction 节（SLA/mediator_rebuild_last_n/responder_fallback）。
2. 新模块 orchestrator/responder.py（或 console.py 扩展，按单一职责裁量）：open_responder_read_conn
   （mode=ro+authorizer 全写拒）+ grounding 校验 + 模板回退 + Mediator（重建）。
3. status_card 接线：advancer 阶段边界发布（原子写文件）+ latest_decision 补真源。
4. 测试：写拒负例矩阵/grounding 负例/重建一致/SLA 计时断言。
5. §5 循环：内审 Opus 子代理 → codex 外审 ≤2 轮（模式 B 后台）→ commit → build_log 0027。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；APPROVE 才 commit；**外审 diff 排除记账类**（`':(exclude)build_log/**' ':(exclude)implement_note.md'`）。
- **本 harness 署名**：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`（勿沿用旧 session 的 Opus 4.8 行）。
- **CP6.1 评审教训**（M5 人机窗口高发面）：读-改必须同事务（TOCTOU）；状态谓词跨消费生命周期要自洽
  （pause 消费≠失效）；幂等重放返回值须与首次**等价**（含 needs_confirmation）；队列 API 别吐调用方
  必拒的项。
- gate/文件库测试须**文件库**（authorizer 读连接独立；responder 的 mode=ro 连接同理）；statestore/
  console 可 :memory:。
- DDL 字节冻结：改 schema 须同步 database.py MIGRATION_SHA256（走评审）。policy.yaml/schema 改动=决策性。
  status_card/outbox 是派生/实现层，**不在核心 DDL**（发布走文件，勿建表）。
- 测试基线现 **454**。
- 悬案/M6 硬化清单（build_log 0023–0026 遗留汇总）：注册段合一事务、bundle pc 产物集哈希锚、route plan
  后特化、attack/import 注册骨架共享、完整供应链 manifest、修复重评轮数、report 制品、materialize_failed
  重试、resolve_deps dead_end 悬案、compiler 检索区接 recall、console 效果接线（set_budget/reprioritize/
  goal_amend 真效果 + 真 Codex 润色/语义分类）、prune_branch 子树级联、多轮并发 abort。
