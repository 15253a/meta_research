# 0044 · CP9.3 人类控制台入站闭环（ConsoleInboxIngest + precheck 装配）

- date: 2026-07-09
- commit: 6967dc3 — feat: CP9.3 人类控制台入站闭环（ConsoleInboxIngest + precheck 装配）
- branch: main
- 检查点 / 步: CP9.3（属：步⑨ M8 人类控制台接入——系统在控制台上可查看/可交互）

## 决策
**做了什么**：打通控制台**入站闭环**。CP9.1 落了 console_server（POST /api/message 把运维命令写
`<work>/state/console_inbox.jsonl`），CP9.2 落了前端。本检查点让 run 进程在 **precheck 边界**把这些行
ingest 进权威入站链：`Console.handle_inbound`（幂等落 directive/note）→ intent=query 再
`Mediator.handle_query`（写 interaction_reply 应答）。至此运维在控制台敲的命令能真正被系统消费。

**为什么这么设计**：
- **权威在 DB、spool 只是缓冲**：console_inbox.jsonl 是连接器缓冲、非权威；correctness 由 interaction_message
  UNIQUE(connector,idempotency_key) + interaction_reply 存在性保证，游标只是避免重扫的纯优化。
- **入站是辅助面，绝不拖垮自动推进主循环**：一切故障有界处理、顶层兜底，不让人类输入崩掉全自动 loop。
- **no-loss / no-dup 双保证**（经三轮评审收敛）：query-once 落**持久层**（应答前查 reply 是否已存在），
  **只有 durable reply 才推进游标**（终态回执也写不进则不推进）——既不重复回复也不漏答。

**影响面**：新增 1 只读桥模块 + run.py 装配 2 行 + console_server 1 行注释。不改编排器/研究状态机。
单写纪律不破：run 进程独占 cursor + DB 写；console_server 只 append spool + mode=ro 读。

## 改动文件
- `meta-research/orchestrator/console_ingest.py` — 新增：`ConsoleInboxIngest`。
  - `ingest(cyc)`：顶层 try/except 兜底 → `_ingest`：读 spool（drop torn-tail 未终止尾行）→ **line-index 游标**
    （已消费的已提交行数）逐行 dispatch；retry→停批不推进、ok/poison→行数照进。
  - `_dispatch`：坏 JSON / **非对象**（"x"/[]/3）→ poison（跳过并推进，防 rec.get 抛后每拍重扫）。
  - `_process_one`：handle_inbound（OperationalError 有限重试(5)→超限跳过推进[raw durable]；其余异常=毒→推进）；
    query 转 `_answer_query`。
  - `_answer_query`（**no-loss 不变量**）：已答→推进；未答→应答；失败→有限重试；超限→写终态失败回执，
    **仅回执/原答 durable 才推进**，否则 retry（DB 持续故障时不推进无害——那时主循环本身也停）。
  - `_bump`/`_has_reply`；`_attempts`（进程内，按 idempotency_key；跨重启归零的 liveness 弱化已在注释说明取舍）。
- `meta-research/orchestrator/run.py` — 修改：build_system 加 `mediator = Mediator(daemon, status_card.json)` +
  `inbox_ingest = ConsoleInboxIngest(console, mediator, work)`；包裹 precheck（ingest 先跑、再 base_precheck）。
- `meta-research/orchestrator/console_server.py` — 修改：enqueue_message 加**单实例假设**注释（多实例撞
  idempotency_key 会被幂等层当重放吞第二条）。
- `meta-research/tests/test_console_ingest.py` — 新增（20 例，见验证）。

## Review（codex-chatgpt gpt-5.5/xhigh；两轮上限 §2.2）
- 内审（Opus 子代理）：**1 BLOCKER**（query-once 曾依赖游标——handle_query 非幂等，游标丢/崩溃重放会重复回复）
  + 3 SHOULD（丢可重试消息 / I/O 逃逸崩循环 / 测试缺口）+ 1 NIT（单实例注释）——**全改**。
- 外审第1轮：**REQUEST_CHANGES**。1 BLOCKER（query 非 OperationalError 异常吞后推进→永久漏答，真触发=
  latest_card 缺卡抛 FileNotFoundError）+ 2 SHOULD（OperationalError 非恒瞬时会饿死 / max-seq 游标消费不了无 seq 坏尾行）
  ——**全改**：有限重试+终态回执、line-index 游标。
- 外审第2轮：**REQUEST_CHANGES**（第2轮=最后一轮）。1 BLOCKER（终态回执**写失败**仍推进→no-loss 未闭合）+ 3 SHOULD
  （_has_reply 失败逃逸 / 非对象 JSON 卡队列 / 跨重启 attempts 归零 liveness）+ 1 NIT（注释不准）。
  **按 §2.2 第2轮后自行修毕、不再送审**：no-loss 闭合（仅 durable reply 才推进）、_has_reply 纳入重试模型、
  非对象守卫、注释订正；跨重启 attempts 持久化按取舍**记注释留待**（持久故障通常=系统整体已停、阻塞 ingest 无害）。
- 未采纳意见及理由：仅「跨重启 attempts 持久化」暂缓（理由如上，注释在案）；其余全采纳。

## 验证
- 命令：`python -m pytest -q`
- 关键输出：
  ```
  662 passed in 119.66s
  ```
- test_console_ingest.py 20 例覆盖：directive 落 pending / query 写 reply / 幂等重放 + 游标 / 游标丢重放安全 /
  torn-tail 延迟 / 坏 JSON 跳过 / **非对象 JSON 跳过** / 可重试(OperationalError)不推进-下轮成功 / 顶层 I/O 兜底 /
  **query-once 游标丢不双答** / 多 query / cyc 绑 created_cycle / **卡片未发布重试后恰一次答** / **持久故障写终态回执** /
  **终态回执失败绝不推进（no-loss 闭合）** / 无序号坏尾行被行数游标消费 / pause→确认→阻断→resume→确认→解阻断 端到端。
- 步级验证（步⑨未收尾）：本检查点不收尾步⑨（CP9.4 收口），故不跑步级验证。
- 结论：**通过**。

## 遗留 / 回退
- 待办：**CP9.4**（步⑨收尾）：真 Codex 跑 + console_server 并行 + 浏览器/HEADLESS 真查看实测 + README 控制台节 +
  步⑨步级验证收口。
- 已知取舍：`_attempts` 不跨重启持久化——run 进程在达重试上限前**反复重启**时，同一持久故障消息的 give-up 计数归零、
  理论上可长期阻塞后续行（no-loss/no-dup 不受影响）。判定：持久故障通常意味 DB/系统整体已停，阻塞 ingest 无害 →
  v1 不持久化，注释在案，需要跨重启 liveness 契约时再补。
- 存量（非阻塞）：CP8.6b（eval/import/route）；运维执行/硬化。
- 回退：`git revert 6967dc3`（新桥模块 + 装配 2 行 + 注释，无编排器耦合，安全独立回退——回退后控制台入站不消费，
  其余不受影响）。
