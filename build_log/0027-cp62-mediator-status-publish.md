# 0027 · CP6.2 query 只读应答链 + 中介重建 + status_card 发布（M5）

- date: 2026-07-08
- commit: 28c6117 — feat: CP6.2 query 只读应答链 + 中介重建 + status_card 发布（M5）
- branch: main
- 检查点 / 步: CP6.2（属：步⑥ M5 人类控制台 + query 只读应答器）

## 决策
落 §7.1 M5 的 query 侧四件：**只读边界（双保险）→ grounding 机械闸门 → 中介可重建 → 阶段边界发布**。

- **mediator.py（新）**：open_responder_read_conn = mode=ro URI（物理只读）+ authorizer 全写类动作
  DENY（INSERT/UPDATE/DELETE/DDL/**CREATE·DROP VTABLE**/ATTACH/PRAGMA；TRANSACTION/SAVEPOINT 故意
  放行——发布器在该连接 BEGIN 钉读快照）。**temp schema 在 mode=ro 下仍可写**，TEMP*/VTABLE 必须靠
  authorizer 拒（外审 BLOCKER）。grounding_check 四规则：状态已变声称/引日志作证/自产 directive 声称/
  卡外实体引用——实体匹配用 ASCII-字母数字环视 + 大小写不敏感（CJK 是 \w、"轮c7" 无 \b 边界会漏检
  [内审 BLOCKER]；"Q99" 写法不绕过[外审 NIT]）+ 实体对实体比对（防 "c1"⊂"c12" 子串假过）。
  TemplateResponder（确定性模板、构造性接地、kind='template' 显式类属性）；Mediator.handle_query
  = **按 message_id 从持久层取查询文本**（外审 SHOULD：不信调用方复述，回复与输入同锚 message_id，
  审计链可重建）→ 读**发布快照**（非在途 DB）→ 消毒 → 应答 → grounding 不过退 render_fallback →
  经 InteractionIngest.ack 入库（append-only，不写 decision，P1）；缺 kind / 非 template kind 一律
  NotImplementedError（M6 codex kind 须绑 runner_call phase=interaction_query，防静默错标）。
  rebuild = 最近发布卡 + 同 connector 最近 N 条消毒历史，**相关子查询取每条 message 最新回复**
  （外审 SHOULD：LEFT JOIN 在 ACK+应答双回复时扇出、LIMIT 误截）；应答确定性 ⇒ 重建前后回答逐字节
  一致。应答器本体只收字符串、无任何 DB 连接（比 mode=ro 更强）。
- **status_card.py**：latest_decision 接线（清 M2 TODO）——本 cycle 作用域最近 decision 摘要
  {id,actor,type}（非全局 LIMIT 1，防跨轮/跨 goal 串卡）+ SqliteStatusPublisher（tmp→os.replace
  原子发布 latest 卡；发布失败 fail loud，重试/降级=M6 硬化）。
- **advancer.py**：可选 status_publisher 装配；run_cycles 每格 advance 后发布（§4.6.6 阶段边界）。
  发布是派生观测、不参与研究状态机 → 可选装、advance 抛错则不发布。

## 改动文件
- `meta-research/orchestrator/mediator.py` — 新增：authorizer/ro 连接 + grounding + TemplateResponder +
  render_fallback + Mediator（handle_query/rebuild/latest_card）。
- `meta-research/orchestrator/status_card.py` — 修改：latest_decision 按 cycle scope 接线；新增
  SqliteStatusPublisher；文档同步。
- `meta-research/orchestrator/advancer.py` — 修改：__init__ 增 status_publisher 参数 + run_cycles
  每格发布 + _publish_card。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图加 mediator.py 行。
- `meta-research/tests/test_mediator.py` — 新增：15 测（写拒负例矩阵/temp-schema+VTABLE 拒[断言
  not authorized]/裸 mode=ro/grounding 三类声称+卡外实体+CJK 无空格+大小写回归/幻觉应答器退模板/
  缺 kind fail-loud+未知 message 拒/读发布快照非在途 DB/发布原子/advancer 接线端到端/重建一致/
  多回复无扇出/重建窗口/p95 SLA）。
- `meta-research/tests/test_status_card.py` — 修改：test_selection_from_cycle 语义更新 + 新增
  test_latest_decision_cycle_scoped。
- `implement_note.md` — 记账：现场快照随检查点入库。

## Review
- 内审（Opus 子代理）：REQUEST_CHANGES → 全修：B1[BLOCKER] grounding 实体规则被 CJK 紧邻绕过
  （"轮c7" 无 \b 边界漏检；系统全中文语境=常态非边角）→ 环视实现+无空格回归；S1 responder_kind
  静默错标缝 → kind 属性+入库闸；N1 子串假过 → 实体对实体；N2 None 渲染 → '未定'。其余 9 个关注面
  （authorizer 完备性/TRANSACTION 放行/mode=ro+WAL/cycle 作用域/发布原子/读快照非在途）逐项实证通过。
- codex（gpt-5.5/xhigh）第1轮：REQUEST_CHANGES——1 BLOCKER + 3 SHOULD + 2 NIT，全部采纳修复：
  ①[BLOCKER] authorizer 漏 CREATE/DROP VTABLE（temp schema 在 mode=ro 下仍可写，VTABLE 可落 temp
  绕过只读边界）→ 补常量 + temp-schema 覆盖测试；②[SHOULD] 缺 kind 静默当 template → 默认 None、
  fail loud；③[SHOULD] handle_query 信任调用方 raw_text 断审计链 → 签名改 (message_id)、文本按 id
  从持久层取；④[SHOULD] rebuild LEFT JOIN 多回复扇出 → 相关子查询取最新回复、窗口按 message 数
  （+回归）；⑤[NIT] ro 连接 isolation_level=None；⑥[NIT] fallback None 渲染。
- codex 第2轮：**APPROVE**。随附 2 NIT 亦已修：实体匹配大小写不敏感（Q99 不绕过）；VTABLE 测试改用
  不存在模块名 + 断言 not authorized（证明拒在 authorizer 而非环境缺 FTS5——实测 authorizer 在
  prepare 期先于模块解析）。
- 未采纳意见：无。

## 验证
- 命令：`python -m pytest tests/test_mediator.py tests/test_status_card.py -q` → **27 passed**；
  `python -m pytest tests/ -q` → **470 passed**（CP6.1 后基线 454 + 16 新，无回归）。
- 结论：通过。

## 遗留 / 回退
- 待办：CP6.3 通知矩阵 outbox + 文件请求全流水 + 全局等待 + Advancer 前置检查接 console（先消费
  到期 directive → 再查 has_blocking_pause / pending 文件请求）→ 收尾 M5 跑步级验证。M6：真 Codex
  应答器（kind='codex'+runner_call 绑定+grounding 唯一入库闸门）、发布失败重试、p95 压测口径。
- 回退：`git revert 28c6117`（mediator 新模块+status_card/advancer 增量，回退不波及 CP6.1 及以前链路）。
