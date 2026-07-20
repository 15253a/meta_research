# SKILL · reasoning —— 轮尾收口：答题 · 树维护 · 选题 · 收尾

> 版本：m4-cp114c3d14。按《第一部分》§3.5 与流程图 02-Reasoning；产物 schema =
> `schemas/answer.schema.json` / `schemas/tree_ops.schema.json` / `schemas/selection.schema.json`。
> reasoning 是裁决与作答的**唯一收口**，每个 cycle 都必须在轮尾调用一次；dependency_wait、Idea/Plan
> 失败和 Bundle replan 也不能跳过。
> 正常路径只有一个 Reasoning 主 Codex 和一个连续 turn：它同时处理该 cycle 的全部分支、结论、
> tree ops、selection 与 cycle summary，并在同一 turn 内修正 MCP/schema 拒绝。不得为某个分支另开顶层
> Reasoning，也不得由 Idea/Plan/Bundle 或普通 decision 写入直接终态化 cycle/question。

## 通用

- **触发条件**：攻坚轮 bundle 结束（含早退或 replan）/ dependency_wait / reuse_only 轮 plan 后 / reasoning-only 轮
  （bootstrap / decompose / goal_amend）。
- **读取**：本轮问题卡 Qn + **本轮 bundle 结果**（evaluation + metric_results + 失败摘要 +
  运行观测摘要）· 目标当前版全文 · 子树摘要卡 · 待消费 directive · 召回（相似结论带
  applicability 徽标 / 可对比 evaluation）。
- **门禁与写入**：answer+evidence 经 gate_close_question（I3）；tree_ops 经 apply_tree_ops
  （封闭七词表）；selection 经 persist_selection。收尾（cycle_report / views / 记账 / 通知）
  由编排器在提交后执行——你只产 JSON + md。
- **失败语义**：产物结构、引用或提交预检非法时，runtime MCP 在当前 turn 返回精确错误，
  Reasoning 主智能体就地修正并重提。编排器不以 `artifact_parse` 为理由递归调用另一个模型；
  无法在该 turn 中产生完整回执时轮次才 fail closed（cycle=failed，Qn 回 open）。证据不足 ≠ 失败：
  走 inconclusive 路径（见 R1）。

## 输出 files 约定（按轮型取舍）

- 攻坚/复用轮：`answer.json`（仅 verdict ∈ {answered, refuted} 时产；证据不足**不产**）+
  `tree_ops.json`（可为空 ops）+ `selection.json`（必产）。
- reasoning-only 轮：不产 answer.json；tree_ops 按轮型（见下）；selection 必产。
- dependency_wait：不产 answer.json，`tree_ops.ops=[]`；总结本轮为何登记依赖以及满足后应如何继续。
  selection 只作 advisory（系统保持等待 exact dependency），不得因为等待就 terminate。
- `md` = cycle_report 正文草稿（中文：本轮做了什么 / 证据怎么读 / 为什么这么选下一步）。

## Question 准入（所有建题 op 的硬语义）

question 是一个**可由 I3 允许证据回答或反驳的研究问题**，不是待办事项。每个
`create_root`、`add_children.children[]`、`spawn_question` 都必须同时给：

- `text`：研究者要回答的语义问题；
- `predicate_json`：`{"kind":"evidence_closure_v1","allowed_evidence":[…],
  "answer_criterion_md":"…","refute_criterion_md":"…"}`。`allowed_evidence` 只能从
  `evaluation / literature / child_answer / human` 选择，且关闭判据必须说明什么观察分别支持肯定与否定回答。

立题下界是一条可关闭的测量、复用判断或子答案聚合。**目录/文件/资产盘点、读日志、修代码、处理报错、
安装依赖、权限/环境配置、部署恢复等工程动作永不立题**；它们留在 plan/bundle 的实施与自愈记录、
failure summary、DECISION 或 cycle_report。`engineering_blocked` 只能说明当前研究问题本轮缺少有效证据，
不得据此 `spawn_question` 一个“如何修复”的工程节点。“值不值得做/下一步该做什么”属于 selection/DECISION，
也不立题。idea 是已存在 question 下的候选研究路径，不能反向把 idea 或工程失败包装成 question。

## 【轮尾任务】步骤

**先判轮型**（context_pack 固定锚标明 route）：

- **dependency_wait**：本轮 plan 已登记一个待物化 baseline/repository 依赖，没有形成新研究证据。
  不关闭/反驳问题，不创建新题，不把等待记作 inconclusive；只总结本轮 cycle、说明依赖满足/blocked 后的行动，
  `tree_ops={"ops":[]}`，selection 选择一个非 terminate 的后续研究意向。系统会保存该建议并继续等待依赖。

- **bootstrap**：只做「出首题」——tree_ops 恰一条 op，**精确键**：
  `{"op":"create_root", "local_key":"root", "text":"<首题一句话>",
  "predicate_json":{"kind":"evidence_closure_v1","allowed_evidence":["evaluation","child_answer"],
  "answer_criterion_md":"<肯定关闭判据>","refute_criterion_md":"<否定关闭判据>"}, "rationale_md"?}`
  （问题正文的键是 **text**，不是 question_md/题面等自造名）；selection 选它，
  **intent 按 R3 预算规则定**：est_cost ≤ B(t) → attack；**est_cost > B(t) → decompose**
  （下一轮分解首题，别硬塞进一轮）。`next_question_id` 填该 local_key（编排器同事务
  解析为真实 id）。不答题。
- **decompose**：对选中的超预算问题写**仍可各自证据关闭**的子问题（`add_children.children[]` 每项均带
  `text + predicate_json`，受树规模护栏：max_children_per_node / max_decompose_depth）+ selection
  选一个子问题攻坚。不答题。不得把父问题所需的目录盘点、编码、运行或部署步骤拆成子问题。
- **goal_amend**：只做目标改版，不答题。`tree_ops.ops[0]` **必须且只能是一个**
  `amend_goal`；其中 `new_goal_text / predicate_json / rationale_md` 必须逐字段复制固定锚里
  已确认的 goal_amend directive，禁止自行润色、补写或换谓词。升版后才可追加：
  `seed_applicability_audit`（只选可能受新根谓词影响的旧版 closed answer，受
  `max_closed_revalidate_per_cycle`）以及按需 `spawn_question(kind=goal_retarget/revalidate)`（新题同样必须带
  evidence closure `predicate_json`）
  （受 `max_spawn_from_goal_amend` 和树护栏）。**绝不重开 closed 问题**；旧 answer/evidence
  保留出生版本，跨版本 child_answer 复用必须先得到 `still_applicable`。selection 必须对固定锚
  给出的新版全部可调度 open/inconclusive 问题（含本批新题）逐一重打分，不能沿用旧 score；
  无法派生新问题且没有可复用旧结论时 terminate，编排器会记 `blocked_by_goal_amend`。
- **攻坚 / reuse_only / eval_only（正常轮尾）**：走 R1–R4：

**R1 答题**：解释本轮证据（指标可信度、失败模式；运行观测摘要只影响"可信度/下一步"，
**永不作为结论证据、不得据 log 判 novelty/success**）。据证据对 Qn 下 verdict：
- answered / refuted → 产 answer.json：`question_id` + `verdict` + `answer_md` +
  `evidence[]`。**evidence 四分支的精确键**（每分支只带自己的键 + 可选 `note_md`，
  混带即被 schema 拒）：`{"kind":"evaluation","metric_result_id":"mrN"}`（指向**成功测量**；
  必须从固定锚 `evidence_ref=mrN` **只复制 `mrN`**，旁边的 metric/version/value/scope 是展示元数据，
  绝不是 id 的一部分）·
  `{"kind":"literature","citation_md":…}` · `{"kind":"child_answer","child_question_id":…,
  "child_answer_ref"?:…}` · `{"kind":"human","human_ref":…}`。
  是否注明 fake **只看该测量在固定锚中的显式 provenance**：仅当锚明确给出
  `evaluation.source=fake`、`source=fake` 或 `synthetic=true`（布尔大小写不敏感）时才注明 fake。
  SQLite production 的 `successful_measurements` 未带上述标记时不得称为 fake；也不得从目标里的
  M0/M6 文案、skill 版本名或旧说明自行推断。
- 证据不足 / 本轮失败（idea 全不合格、plan 评审不过、关键目标失败、engineering_blocked）→
  **不产 answer.json**，md 写明缺什么证据；编排器将置 Qn inconclusive（visit+1）。

**R2 树维护**（tree_ops，按需；关键必填键点名）：spawn_question（kind ∈ diagnosis/followup/
revalidate/import_reference，须带 `text + predicate_json + parent_question_id`——诊断/后续挂触发它的问题、revalidate
挂被回看问题；goal_retarget 则 `parent_question_id=null`）· mark_answer_applicability（带
`answer_id`+`status`+`rationale_md`；needs_revalidation/contradicted 必须同批 spawn revalidate
题并以 `spawned_question_ref` 回指其 local_key）· propose_prune（带 `question_id`+`reason_md`）·
消费 inject_question / reprioritize / prune_branch / note 类 directive。

固定锚若给出 `reasoning_question_request`，接受该请求时必须产一条 `spawn_question`，并把其中
`request_ref`、`requested_text`（改键名为 `text`）、`parent_question_id`、`suggested_kind`/`kind`
逐字复制；只允许自行补充 evidence-closure `predicate_json`、`local_key` 与理由。不得省略/改写
`request_ref` 后另建一条“看起来相同”的题。StateStore 会在建题同一事务内核验 exact
text/parent/kind/current-goal/provenance，绑定已消费 console directive 或
`import_trigger_completed`，并为 human_named/冻结参照建立后续 import authority；不一致会整批回滚。
`parent_question_id=null` 仅允许固定 console request_ref 明确如此时照抄，普通自主 spawn 仍须有 parent。

**R3 选择出题**（selection，必产）：综合当前证据缺口、预期信息增益、成本、访问次数与用户优先级，
选择最值得推进的一题。不要为模仿公式而制造一张完整评分表；模型的核心交接只有
`next_question_id` 与 `next_intent`。估计成本不超过 `decompose_threshold×B(t)` 时选 attack，超过时选
decompose；终止三判据（连续低分 / 预算耗尽 / 目标谓词满足）任一满足才选 terminate。
可选集 = 固定锚给出的可调度问题集（含本轮 Qn——若它被置 inconclusive）∪ 同批 tree_ops
新建题（用 local_key 引用）。**证据不足 ≠ 终止**：Qn 没答上来时通常重选 Qn（换 idea 再攻）
或 decompose，terminate 只认三判据（连续低分 / 预算耗尽 / 目标谓词满足）。
**selection.json 最小键**：`next_question_id`（terminate 时为 null）+ `next_intent`；terminate 时再给
`terminate_reason_md`。`scores` 是可选的**增量状态更新**：只有本轮确实形成了新的可比较估计时才写，
每项至少 `question_id / score / est_cost`，其余分解字段只在有独立信息时写。普通轮不要求覆盖整个前沿，
也不要重复上下文中的 visit；仅 goal_amend 因版本切换仍须按前文要求重评全部可调度前沿。
**你只产 selection、不写 route**（route 由编排器派生）。

**R4 收尾**：md 写 cycle_report 正文（中文），含：本轮四阶段结果一句话链、证据与结论、
打分表摘要、下一步与理由。随后先调用 `record_cycle_summary` 写紧凑索引，再在当前 Reasoning 主会话调用
`submit_stage_artifact` 提交 selection/tree_ops/answer 与 md；工具返回错误时就地修改并重提，直到成功。
核心 answer/question/tree/selection 事务仍由编排器消费成功 path/hash 回执后，在 Reasoning 核心短事务中
重新核对 cycle/question 未终态和身份绑定，然后一次性提交。**只有这个核心事务可以终态化本 cycle、
关闭/重置 question 并确定后续 selection**；Bundle replan、Idea/Plan 失败、分支 decision 和 summary 索引都不能代替它。
