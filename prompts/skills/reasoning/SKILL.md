# SKILL · reasoning —— 轮尾收口：答题 · 树维护 · 选题 · 收尾

> 版本：m0-1。按《第一部分》§3.5 与流程图 02-Reasoning；产物 schema =
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

- **bootstrap**：只做「出首题」——tree_ops=create_root（首题直接对齐 goal 成功谓词、
  可由证据关闭；**带 `local_key`，如 "root"**）+ selection 选它攻坚
  （`next_question_id` 填该 local_key，编排器同事务解析为真实 id）。不答题。
- **decompose**：对选中的超预算问题写子问题（add_children，受树规模护栏：
  max_children_per_node / max_decompose_depth）+ selection 选一个子问题攻坚。不答题。
- **goal_amend**：消费 goal_amend directive → tree_ops=amend_goal（+按需
  seed_applicability_audit，受 max_closed_revalidate_per_cycle）+ selection。不答题。
- **攻坚 / reuse_only / eval_only（正常轮尾）**：走 R1–R4：

**R1 答题**：解释本轮证据（指标可信度、失败模式；运行观测摘要只影响"可信度/下一步"，
**永不作为结论证据、不得据 log 判 novelty/success**）。据证据对 Qn 下 verdict：
- answered / refuted → 产 answer.json：`question_id` + `verdict` + `answer_md` +
  `evidence[]`。**evidence 四分支的精确键**（每分支只带自己的键 + 可选 `note_md`，
  混带即被 schema 拒）：`{"kind":"evaluation","metric_result_id":…}`（指向**成功测量**）·
  `{"kind":"literature","citation_md":…}` · `{"kind":"child_answer","child_question_id":…,
  "child_answer_ref"?:…}` · `{"kind":"human","human_ref":…}`。
  M0 假执行的测量可作流程性证据（结论正文须注明 fake）。
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
next_intent：最优问题 est_cost ≤ B(t) → attack；> B(t) → decompose（选该问题）；终止三判据
（连续低分 / 预算耗尽 / 目标谓词满足）任一满足 → terminate（next_question_id=null +
terminate_reason_md）。**你只产 selection、不写 route**（route 由编排器派生）。

**R4 收尾**：md 写 cycle_report 正文（中文），含：本轮四阶段结果一句话链、证据与结论、
打分表摘要、下一步与理由。
