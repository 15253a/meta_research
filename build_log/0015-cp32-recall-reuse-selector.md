# 0015 · CP3.2 Recall 四级可停 + 复用判定 O(1) selector

- date: 2026-07-08
- commit: 1a099fc — feat: CP3.2 Recall 四级可停 + 复用判定 O(1) selector（M2）
- branch: main
- 检查点 / 步: CP3.2（属：步③ M2 上下文编译器 + 召回 + 观测摘要 + status_card）

## 决策
M2 第二检查点：交付**召回**与**复用判定**的真实现，与 M0 StubRecall/StubCtx 并存不替换（M0 driver 仍走桩、基线绿；M3 Advancer / 编译器检索区接真）。两大能力：

1. **复用判定 O(1)（§4.1.5，M2 验收核心）**：`reuse_selector` 判「同 variant + protocol@ver 的成功 evaluation 是否已产齐所需测量」→ 命中即零执行引用历史。规范 SQL 逐条对齐 §4.1.5：evaluation.status='success' + attempt.status='success' + attempt.env_hash **精确**匹配 + `parser_result_suspect(attempt)=0` + 来源 build_target(`COALESCE(attempt.bt, eval.bt)`) NULL 或 complete + 每个 required (metric_id,metric_ver) 有 metric_result(scope='aggregate')；`ROW_NUMBER PARTITION BY (metric_id,metric_ver) ORDER BY canonical DESC, attempt_no DESC` 取 rk=1；命中 ⟺ 返回行覆盖全部 required。`explain_reuse` 出 EXPLAIN QUERY PLAN 供验收断言走测量索引、非全表扫。
2. **渐进四级召回（§3.6.2，可停）**：①卡片初筛（card_md LIKE 作 v1 语义 stand-in + `baseline_tag` faceted 匹配）②变体矩阵中筛 ③测量索引精确 O(1) ④ctx-fetch 深潜（`SqliteCtx.fetch` 读 execution_log ref+hash 元信息）。每级独立可停。

裁量（全自动）：
- `parser_result_suspect` 是 parser 据 execution_observation 派生的负向谓词——M2 无真 parser（M4 落）→ 注册桩恒返 0（deterministic=True，不挡任何复用）；**不自动注册**（否则覆盖 M4 真派生），未注册连接跑 selector → 抛可行动 RuntimeError。
- 嵌入语义召回 defer（无模型）→ 语义检索用 card_md LIKE stand-in；但 **faceted tag 无需模型、不 defer**，据 §3.6.2 第1级「语义检索 + faceted tag」实现（tag 挂 baseline 级 `baseline_tag`，§4.5.3）。

## 改动文件
- `meta-research/orchestrator/recall_sqlite.py` — 新增：`register_parser_suspect_stub` / `reuse_selector` / `explain_reuse` / `_reuse_query`（required VALUES 参数序在前）/ `_exec_with_suspect`（未注册桩→可行动错误）/ `SqliteCtx`（execlog:<id> isascii+isdigit 双护、未知 ref 诚实）/ `SqliteRecall`（level1 卡片 LIKE+faceted tag、level2 变体矩阵、level3 复用、query 入口）。
- `meta-research/tests/test_recall_sqlite.py` — 新增：15 测（复用命中/未命中×4[env/metric/非success/target 未complete]/EXPLAIN 走测量索引+mr uq_mr_agg+无 SCAN e·ea·mr/未注册桩报错/四级召回/faceted tag 独立命中/Ctx fetch）。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图加 `recall_sqlite` 行。

## Review（codex-chatgpt gpt-5.5/xhigh）
- 第1轮：**APPROVE**（无 BLOCKER）。§4.1.5 逐条核可（required 参数序在前、env 精确、success 约束、COALESCE build_target complete、scope=aggregate、hit=covered.issuperset）。1 SHOULD：level1 文档宣称「LIKE + tag」但 SQL 只搜 card_md——若 tag 是硬验收应实现 + 加测试。1 NIT：EXPLAIN `SEARCH mr USING INDEX` 版本字串敏感。
- 采纳 SHOULD：确认 §3.6.2 第1级明文含 faceted tag、`baseline_tag` 已在冻结 DDL、无需模型 → 实现 tag 匹配（基线卡 ref_id=baseline.id，其 baseline_tag.tag LIKE query 亦命中，即使 card_md 不含）+ 加 test_recall_level1_faceted_tag。NIT 未采纳：保留正向 mr 索引断言（内审要求证 O(1) 全链；职责即抓计划回归），并保留 codex 更看重的 SCAN 负向护栏（覆盖 e/ea/mr 三别名）。
- 第2轮：**APPROVE**（零 BLOCKER/SHOULD/NIT）。复核新 faceted tag SQL：`bt.baseline_id=c.ref_id` 被 `c.card_type='baseline'` 守住（family/protocol 卡 ref_id 碰撞 baseline id 也不误召回）、参数序 (like,like,k) 对、LIKE 参数化无注入、EXISTS 避免多 tag 重复行、ORDER BY c.id 定序稳。

## 验证
- 命令：`pytest tests/ -q`
- 关键输出：
  ```
  298 passed in 15.11s
  ```
- 复用判定 O(1) 证据（`explain_reuse` 实测 EXPLAIN QUERY PLAN）：
  ```
  SEARCH e USING INDEX sqlite_autoindex_evaluation_1 (variant_id=? AND protocol_id=? AND protocol_ver=?)
  SEARCH ea USING INDEX sqlite_autoindex_evaluation_attempt_1 (evaluation_id=?)
  SEARCH mr USING INDEX uq_mr_agg (evaluation_attempt_id=? AND metric_id=? AND metric_ver=?)
  ```
  → 命中走测量索引 (variant,protocol,ver)、mr 走部分唯一索引 uq_mr_agg，无 `SCAN e/ea/mr`（仅 CTE r/ranked/subquery SCAN，符合预期）。
- 结论：通过。（步级验证 = M2 整步收尾在 CP3.3，本检查点未收尾步。）

## 遗留 / 回退
- 待办：复用判定**不得在 M4 真执行前上线**——`parser_result_suspect` M2 恒 0，真派生 M4 落（否则可能复用存疑测量）。CP3.3 收尾 M2（观测摘要进锚点 + status_card + authorizer 负例 + import deferred 不产 target 断言）。
- 回退：`git revert 1a099fc`（新增文件 + __init__ 一行，与 M0 桩并存、无接线，回退不影响基线绿）。
