# db/ —— SQLite 唯一真相（治理说明）

- `migrations/0001_appendix_a.sql`：唯一规范 schema，**逐字摘自** `reference/第一部分-系统架构设计.md`
  附录 A（行 918–1614；v2.2 终稿 + v2.3/v2.4/v2.4.1 增量已并入原文）。
- **字节冻结**：`orchestrator/database.py` 以 SHA256 常量锁定该文件，并在每次建库/开库时校验
  「checksum + 计数（36 表/72 触发器/29 索引含自动/1 视图）+ user_version」。改动 schema =
  决策性改动，须走检查点评审并同步更新冻结锚。
- 运行库 `research.sqlite` 不入 Git（运行时产物）；migration 与本说明入 Git。

## M1 开工时的三项 OPEN 裁定（2026-07-07，用户授权模型裁定；本文件为受审载体）

**裁定 ①（规范内部缺口：《二》§6.12 v2.3 增量旋钮提及 import 的 `selection_key` 排序 /
`policy_hash` / 所需 license scope，而附录 C 无对应键）——维持附录 C 现状，不自行扩键。**
理由：这些旋钮只在 M4 的 import 确定性选择 / 物化时被消费；M1 的 `external_candidate` /
`license_review` / `external_import` 表约束不依赖任何旋钮取值。提前发明键名有与 M4 实设计
冲突的风险；规范对"注册表暂缺、随里程碑补全"已有先例（observation 节，OPEN #5 → M4）。
登记：M4 开工前随 OPEN #5/#6 一并补齐。

**裁定 ②（DB `evaluation.source` 枚举无 'fake'：M1–M3 假执行如何入真实库）——DDL 一字不动；
假 evaluation 以 `source='factory'` 入库（其流程角色确为出厂评估），假执行以三重口径显式披露：**
1. 每个假执行 target 落一条 `DECISION(actor='orchestrator', type='synthetic_execution',
   payload_json 含 build_target/evaluation/run id)`——`decision.type` 为自由文本（§4.3.2），
   无需改枚举；append-only 使"哪些测量产生于假执行期"永久可查。
2. `execution_observation.parser_version='fake-driver'`（source='parser'——假驱动器即"解析器"，
   版本串如实自报；可 SQL 过滤）。
3. 产物文件层保留 `synthetic=true` 标记（bundle_target.json，M0 既有）。
> ⚠️ 「三重披露」是**写路径约定、非 DB 层不变量**：DB 无法区分假 factory 与真 factory evaluation
> （二者 `source` 同为 'factory'）。这三条由代码层（驱动器/gate 写路径）保证并在 M1+ 由代码层用例兜住，
> DDL 不焊、也无法焊。故它不出现在本轮 CP2.1 的 DB 层否定用例集里。
产物 schema 的 `evaluation.source='fake'` 枚举于 M1 移除（原计划 M4，提前消除产物↔DB 口径
分裂）。M4 防假测量回流复用判定：真研究协议为新 `protocol`（测量索引按 (variant,protocol,ver)
命中，假测量天然不命中）+ M4 selector 增 synthetic 过滤；M4 验收「证据可回溯到一次真实
evaluation」兜底。

**裁定 ③（OPEN #1 可选 DDL 三项 + OPEN #2 applicability 同版触发器）——全部不焊入 DDL。**
理由：M1a 验收把附录 A 当字节冻结对象（36/72/29/1 计数 + checksum 锁定），任何增列/加索引/
加触发器都破坏唯一权威计数；且规范原文已把执行点安排在代码层——
- `question_dep` 防重复：落 StateStore 写路径查重（CP2.3，配否定用例）；
- `answer_applicability` 的 `scoped` 语义：以既有六枚举 + `rationale_md` 范围限定表达（§4.6.4 原文即此用法）；
- `v_cycle_timeline`：人侧视图，随 M5 人向面需要再议（同 status_card/outbox 不入核心 schema 的先例，§5.0）；
- OPEN #2 同版负向判据：落 `gate_close_question` 代码层（CP2.2，配否定用例）——与 §4.1.4
  「本轮为 gate 级」的表述一致；触发器层补焊留待 M4 前复审（若届时出现绕 gate 写库的风险面）。
