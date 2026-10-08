# 冻结 Reasoning v2 的历史输出契约

仅在当前请求冻结的 `research_context.schema_ref=meta-research/reasoning-research-context/v2` 时适用。本次操作继续原输出形状，系统不补写或重排其内容；既有接纳与恢复沿原 hash 和绑定。

`causal_interpretation` 的 `target_commit_refs`、`changed_axis_fact_refs`、`held_fixed_fact_refs`、`provenance_refs` 分别采用该冻结 ContextPack 的完整对应数组及顺序。`current_question.prior_accepted_outcome_refs` 采用冻结 `graph_binding.prior_current_question_outcomes` 的 outcome_ref 列表及顺序，空列表写 `[]`。父问题按冻结父链顺序逐项关联 question_ref、impact 和 statement。

每项引用是一个完整精确 ref。冻结来源说明可用依据；实际 evidence 与 attribution_basis_refs 仍由 Agent 按科学内容选择，历史身份本身不证明研究影响或科学支持。
