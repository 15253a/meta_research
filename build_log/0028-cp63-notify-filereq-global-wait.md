# 0028 · CP6.3 通知矩阵 outbox + 文件请求全流水 + 全局等待（收尾 M5）

- date: 2026-07-08
- commit: bee3b9e — feat: CP6.3 通知矩阵 outbox + 文件请求全流水 + 全局等待（收尾 M5）
- branch: main
- 检查点 / 步: CP6.3（属：步⑥ M5 人类控制台 + query 只读应答器 —— 本检查点收尾该步）

## 决策
M5 最后一块：**人机窗口的出站面（通知）+ 资源请求闭环 + 研究执行的全局等待闸**。

- **notify.py（新）**：
  - Outbox = 实现层文件队列（DDL 36 表冻结不建表）：outbox.jsonl 追加 + delivered.log 标记。
    幂等两层：emit 按 event_key 去重（重扫不重排队）；deliver 按标记去重（send 与标记之间崩溃 →
    重发一次 = at-least-once，接收端按 event_key 去重）。send 抛错中断续投（不吞错不乱序）。
  - DirectiveNotifier：**从 DB 扫描派生**（console 不知 outbox，单一职责；崩溃后可重扫全量补发）
    directive 7 态事件：received/classified/pending_confirmation（展示润色稿）/pending_effect
    （预计消费点）/applied（consumed_cycle+效果摘要，取自消费决策 payload）/rejected（附理由：
    用户拒=payload.rejection_reason，系统不从=decline 决策 reason）/superseded。
    event_key=directive:{id}:{state} 确定性 ⇒ 重扫幂等。
  - FileRequestService：create_checked = resource_request schema 校验（缺 attempted_paths/
    failure_reason 拒——"能自己获取的不得请求"自证）+ 三判据（enabled=false / 条目超
    max_items_per_request=10 / 同 goal (pending+resolved)≥max_requests_per_goal=5）→ 落单
    （幂等锚复用 interaction.py）。resolve = uploads/<req>/<item_no>/ 逐文件 sha256 入账 → 复制
    并入 input/user_provided/<req>/ → resolution_json+resolved_message_id **一次性迁终态**
    （trg_ireq_identity_frozen 只许这一跳；条目缺失=unavailable，部分提供也算 resolved）。
    cancel 同 provenance。
  - FileRequestNotifier：3 事件 pending/reminder（每 remind_interval_h 一档，**now_ts 注入**——
    模块不调 wall-clock 保确定性）/resolved（含 cancelled）。
  - make_advancer_precheck（§4.4.1 全局等待 v1）：每格 advance 前①按时机消费到期 directive
    （immediate 恒到期；stage_boundary 每格即边界；reasoning_start 仅当下一格进 reasoning——
    reasoning-only 轮每格即 reasoning，attack 轮 status='bundle' 下一格是 reasoning[attack_stages
    游标已核]）②查阻断：已消费未解除 pause（console.has_blocking_pause）或 pending 文件请求 →
    返回拒因，Advancer 停（不发新研究 Runner 调用、不推阶段；query/通知不走 Advancer、照常）。
- **console.py**：consume_directive cycle_id 可空（开轮前消费 immediate 指令时无在途轮；
  DECISION.cycle_id/consumed_cycle 本可空）。
- **advancer.py**：可选 precheck 装配；run_cycles 开轮前 _blocked(None) + 格间 _blocked(cyc)
  （cyc 每格后刷新供 reasoning_start 时机判定）；拒因记 last_block_reason 供观测。

## 改动文件
- `meta-research/orchestrator/notify.py` — 新增（Outbox/DirectiveNotifier/FileRequestService/
  FileRequestNotifier/make_advancer_precheck）。
- `meta-research/orchestrator/console.py` — 修改：consume_directive cycle_id Optional。
- `meta-research/orchestrator/advancer.py` — 修改：precheck 参数 + _blocked + run_cycles 接线。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图加 notify.py 行。
- `meta-research/tests/test_notify.py` — 新增：12 测（outbox 幂等+故障续投/directive 硬指令全生命周期
  逐态断言[润色稿在确认事件、applied 带消费轮+效果]/rejected 附理由+superseded/schema 拒/三判据负例/
  resolve 全流水 hash+并入+终态冻结/cancel/3 事件分档幂等/全局等待阻断 Advancer 而 query·通知照常/
  阻断期应答/precheck 消费 immediate+DECISION+resume 恢复）。

## Review
- 内审（Opus 子代理）：APPROVE（无 BLOCKER）。2 SHOULD 已修：①outbox 尾行撕裂楔死整个通知子系统
  （append 崩溃半行 → 后续 emit/deliver 全炸，连"重扫重建"恢复路径都断）→ 末行解析失败容忍丢弃
  （半行=未入队）+ emit 先截修半行再追加（防粘行/防变中段坏行）+ 中段坏行仍 fail loud + 回归；
  ②reasoning_start 在 attack bundle→reasoning 边界的时机判定零覆盖（代码已核对 attack_stages 游标
  语义、正确，但无测试防退化）→ _due_timings 提为模块级 + 全矩阵测试（created/idea/plan/reasoning
  不消费、bundle 才消费）。NIT 已修：resolve 排除 symlink（is_file 跟随链接会把 uploads 外内容并入
  输入资产区）、cancel rowcount 兜底对称、死代码 decline 查询删除（rejection_reason 恒在 payload，
  console 两条拒绝路径都写）、emit O(n²) 加进程内 _seen 缓存（单进程单写者假设注明）、人机门控中间态
  扫描窗口语义 docstring。其余关注面（interaction_request UPDATE 列集 vs 三 CHECK+两触发器/
  consume cycle_id=None 合法性/全局等待无死锁/schema 先于 policy/请求哈希锚一致/reminder 分档数学/
  (pending+resolved) 口径）逐项实证通过。
- codex（gpt-5.5/xhigh）第1轮：REQUEST_CHANGES——2 BLOCKER + 3 SHOULD + 1 NIT，全部采纳修复：
  ①[BLOCKER] "完整 JSON 但无尾换行"会先入 _seen、后被 emit 截修丢弃 → 事件永久丢失：committed 判据
  改为**换行终止**（_events 读 raw bytes、未终止末段无论可否解析一律当未入队丢弃，与截修口径一致）；
  ②[BLOCKER] item 目录本身是 symlink 时 rglob/is_dir 跟进外部目录、其内常规文件绕过逐文件检查：
  _regular_files_no_symlink（src 自身 symlink→空 + os.walk(followlinks=False) + 逐文件排 + 终检
  resolve 落点圈定 src 实路径内）；③[SHOULD] quota 先于幂等锚破坏可重试性：幂等查询前移（同 hash
  重试在配额满时返回既有单）；④[SHOULD] 先 hash 源再 copy 存在改写窗口：copy 后对 **dest 字节** hash
  （manifest 锚实际并入的东西）；⑤[SHOULD] resolve/cancel 不校验 provenance 同 goal：_check_provenance
  （消息缺失/goal 为 NULL/goal 不符全拒，fail closed）；⑥[NIT] 不同 hash 第二张 pending 外泄
  uq_ireq_one_pending DDL 错误：转业务拒因。各配回归（+5 测）。
- codex 第2轮：**APPROVE**（确认 5 点全部按正确方向修掉）。随附 1 NIT：reminder 首扫跨多档只发当前档
  不补历史档——**有意取舍不改**（停机后一口气补发一串过期提醒是骚扰非信息；提醒语义=「现在还在等」，
  waited_intervals 已表达等待时长），已注释入代码。
- 未采纳意见：无（NIT 为语义二选一，已择一注明）。

## 验证
- 命令：`python -m pytest tests/test_notify.py -q` → **19 passed**；`python -m pytest tests/ -q` →
  **489 passed**（CP6.2 后基线 470 + 19 新，无回归）。
- **步级验证（§7.1 M5 行，本检查点收尾步⑥）**：M5 四文件联合勾兑
  `python -m pytest tests/test_console.py tests/test_mediator.py tests/test_status_card.py tests/test_notify.py -q`
  → **64 passed**。判据对照：
  1. directive 按时机消费同记 DECISION → test_console 按时机消费 + test_notify precheck 消费
     （immediate 开轮前消费、DECISION actor=human 落账、resume 解除）。
  2. query 只读边界负例 → test_mediator 写拒矩阵（decision/route/question/cycle/metric_result/
     DELETE/directive 全拒）+ temp schema/VTABLE 拒 + 裸 mode=ro 物理只读。
  3. 分类负例（unclear 不自动答不产 directive）→ test_console unclear 负例 + 礼貌式指令回归。
  4. ACK/query p95<2s → test_mediator SLA（20 样本 p95 断言 vs policy 阈值）。
  5. 中介线程重建一致 → test_mediator 重建前后回答逐字节一致 + 多回复无扇出 + 窗口 N。
  6. 润色≠raw + 未确认硬指令 consume 拒 → test_console（raw 不可变/润色稿 payload/未确认拒）。
  7. 通知矩阵逐态推送断言 → test_notify 硬指令 7 态全生命周期逐态断言 + rejected/superseded。
  8. 文件请求全流水 + 负例 → test_notify（schema 拒/三判据/hash 入账并入 user_provided/终态冻结/
     全局等待阻断 Advancer 而 query·通知照常/resolve 后恢复推进）。
- 结论：通过（**步⑥ M5 全判据勾兑完成**）。

## 遗留 / 回退
- 待办（步⑦ M6）：长跑数百轮无漂移 + §7.3 机制剧本 + §7.4 T1/T2 研究任务 + OPEN #4 确认 +
  硬化清单（0023–0027 累积 + 本检查点新增：outbox 轮转/O(n) 队列扫描优化、真 QQ connector、
  reminder 生产驱动循环接 time.time()、uploads 符号链接防护）。
- 回退：`git revert bee3b9e`（notify 新模块 + console/advancer 小增量）。
