# OPEN 登记表

> 依 CLAUDE.md §3：文档内容只允许「机器可校验」或「显式 OPEN」两态。实现推进到 OPEN 项必须停下确认、不得自行填空。每项标注：阻塞哪个里程碑、何时就近确认。
> 建立于 change/2026-07-04-审计修订三块（codex 第 05 轮 `结论: YES`）。

| # | OPEN 项 | 背景（对应契约现状） | 阻塞里程碑 | 何时确认 |
|---|---|---|---|---|
| 1 | 可选 DDL 三项：`question_dep` 防重复 partial unique index / `answer_applicability` 增 `scoped` 枚举值 / `v_cycle_timeline` 只读视图 | 对应契约本轮已以 gate / StateStore / 编译器规则表达（§4.2.4 / §4.5.1 / §4.6.6）；DDL 字节冻结不动 | M1（DDL 冻结前） | M1 开工时随否定用例一并评估 |
| 2 | applicability 同版负向分支的触发器层焊死（`trg_evidence_child` 增同 goal_ver 分支） | 本轮为 gate 级判据（§4.1.4 gate_close_question）；跨版 `still_applicable` 已有触发器 | M1 | M1 开工时随否定用例一并评估 |
| 3 | 聚合轮确定性跳 idea 的 route 派生优化 | 现状：父问题聚合轮走 attack-intent → idea（NEED=否旁路 + 判官）→ plan(targets=[]) → reuse_only，可跑但浪费一次判官调用（§2.3 / §3.3.1） | M3 | M3 开工前就近确认 |
| 4 | paper-gap 问题是否经 `goal.predicate_json`（「论文可写」谓词）入树 | 第一部分现无论文产出阶段；若入范围，paper-gap 题 = 目标谓词缺口审计题（reuse_only / decompose 可解） | M6 | 研究目标书定稿时 |
| 5 | 附录 C 缺 `observation` 节（§4.7 已声明 parser 阈值旋钮：nan / divergence / loss_trend → `parser_result_suspect`，yaml 未登） | 既有缺口（非本轮引入）；随真 parser 落地补全 | M4 | M4 开工前 |
| 6 | M4 物化 worker cycle 的形态：`cycle.route` 表达方式（NULL？复用既有？另行？）、是否产 cycle_report / 记账收尾 | 已定：研究 route 7 形态封闭、不为 worker cycle 新增枚举（§2.3/§3.6.3）；worker cycle 自身表达与收尾**未定** | M4 | M4 开工前（随 import 完整 start/finish gate 流程一并确认） |
