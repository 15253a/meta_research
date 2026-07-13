# SKILL · reasoning —— 轮尾收口：答题 · 树维护 · 选题 · 收尾

> 版本：m4-cp114c3d13。按《第一部分》§3.5 与流程图 02-Reasoning；产物 schema =
> `schemas/answer.schema.json` / `schemas/tree_ops.schema.json` / `schemas/selection.schema.json`。
> reasoning 是裁决与作答的**唯一收口**（轮尾一次调用；dependency_wait 轮不经此、由 plan 机械收尾）。

## 通用

- **触发条件**：攻坚轮 bundle 结束（含早退）/ reuse_only 轮 plan 后 / reasoning-only 轮
  （bootstrap / decompose / goal_amend）。
- **读取**：本轮问题卡 Qn + **本轮 bundle 结果**（evaluation + metric_results + 失败摘要 +
  运行观测摘要）· 目标当前版全文 · 子树摘要卡 · 待消费 directive · 召回（相似结论带
  applicability 徽标 / 可对比 evaluation）。
- **门禁与写入**：answer+evidence 经 gate_close_question（I3）；tree_ops 经 apply_tree_ops
  （封闭七词表）；selection 经 persist_selection。收尾（cycle_report / views / 记账 / 通知）
  由编排器在提交后执行——你只产 JSON + md。
- **失败语义**：产物结构非法 → 编排器重试 ≤2（artifact_parse）→ 仍败 = **轮次失败**
  （cycle=failed，Qn 回 open）。证据不足 ≠ 失败：走 inconclusive 路径（见 R1）。

## 输出 files 约定（按轮型取舍）

- 攻坚/复用轮：`answer.json`（仅 verdict ∈ {answered, refuted} 时产；证据不足**不产**）+
  `tree_ops.json`（可为空 ops）+ `selection.json`（必产）。
- reasoning-only 轮：不产 answer.json；tree_ops 按轮型（见下）；selection 必产。
- `md` = cycle_report 正文草稿（中文：本轮做了什么 / 证据怎么读 / 为什么这么选下一步）。

## 【轮尾任务】步骤

**先判轮型**（context_pack 固定锚标明 route）：

- **bootstrap**：只做「出首题」——tree_ops 恰一条 op，**精确键**：
  `{"op":"create_root", "local_key":"root", "text":"<首题一句话>", "rationale_md"?}`
  （问题正文的键是 **text**，不是 question_md/题面等自造名）；selection 选它，
  **intent 按 R3 预算规则定**：est_cost ≤ B(t) → attack；**est_cost > B(t) → decompose**
  （下一轮分解首题，别硬塞进一轮）。`next_question_id` 填该 local_key（编排器同事务
  解析为真实 id）。不答题。
- **decompose**：对选中的超预算问题写子问题（add_children，受树规模护栏：
  max_children_per_node / max_decompose_depth）+ selection 选一个子问题攻坚。不答题。
- **goal_amend**：只做目标改版，不答题。`tree_ops.ops[0]` **必须且只能是一个**
  `amend_goal`；其中 `new_goal_text / predicate_json / rationale_md` 必须逐字段复制固定锚里
  已确认的 goal_amend directive，禁止自行润色、补写或换谓词。升版后才可追加：
  `seed_applicability_audit`（只选可能受新根谓词影响的旧版 closed answer，受
  `max_closed_revalidate_per_cycle`）以及按需 `spawn_question(kind=goal_retarget/revalidate)`
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
revalidate/import_reference，须带 `parent_question_id`——诊断/后续挂触发它的问题、revalidate
挂被回看问题；goal_retarget 则 `parent_question_id=null`）· mark_answer_applicability（带
`answer_id`+`status`+`rationale_md`；needs_revalidation/contradicted 必须同批 spawn revalidate
题并以 `spawned_question_ref` 回指其 local_key）· propose_prune（带 `question_id`+`reason_md`）·
消费 inject_question / reprioritize / prune_branch / note 类 directive。

**R3 选择出题**（selection，必产）：对 open（含 inconclusive）集**逐个**四元打分并全程留痕：
`score = w1·expected_gain + w2·info_gain − w3·cost_norm + c·√(ln N/(1+visit)) + directive_adjust`
（权重从 context_pack 注入的 policy acquisition 节取；N=总轮数）。与单轮预算 B(t) 比较定
next_intent：最优问题 est_cost ≤ decompose_threshold×B(t) → attack；超过 → decompose
（选该问题；threshold 与 B_t 均在采集打分参数区注入，默认 threshold=1.0）；终止三判据
（连续低分 / 预算耗尽 / 目标谓词满足）任一满足 → terminate。
可选集 = 固定锚给出的可调度问题集（含本轮 Qn——若它被置 inconclusive）∪ 同批 tree_ops
新建题（用 local_key 引用）。**证据不足 ≠ 终止**：Qn 没答上来时通常重选 Qn（换 idea 再攻）
或 decompose，terminate 只认三判据（连续低分 / 预算耗尽 / 目标谓词满足）。
**selection.json 精确键（顶层只许这四个）**：`next_question_id`（terminate 时为 null）·
`next_intent` · `scores`（数组名是 **scores**，不是 scoring/评分）· `terminate_reason_md`
（**仅 terminate 时给字符串；非 terminate 直接省略该键，不要写 null**）。
scores 每项只许：`question_id / score / est_cost / expected_gain / info_gain / cost_norm /
ucb / directive_adjust / rationale_md`（不要自加 visit 等键——visit 已在问题卡里）。
**你只产 selection、不写 route**（route 由编排器派生）。

**R4 收尾**：md 写 cycle_report 正文（中文），含：本轮四阶段结果一句话链、证据与结论、
打分表摘要、下一步与理由。
