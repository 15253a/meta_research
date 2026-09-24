# 研究内容表面职责划分（2026-09-24 六入口集成）

本记录按 2026-09-22 RG／RM 资产设计决定，把四个内容表面的职责拆清：发现、正文读取、执行输入、阶段上下文各归一处，不再为同一材料拼接多套阶段路径。本版职责已在代码中单一化，未做改名或搬迁；重复注入等性能项维持延后。

## 四个表面

| 表面 | 唯一职责 | 对应能力 | 不承担 |
| --- | --- | --- | --- |
| `research_notes`（research_memory.research_notes.read） | 跨阶段交接说明的发现与正文读取：summary 索引 + 按 version_ref 分页读 body | 发现（说明摘要）、正文读取 | 不承担执行输入绑定，不承担 Plan 依据目录 |
| `plan_evidence`（research_graph.plan_evidence.page + research_memory.plan_evidence.read） | Plan 已选依据的发现目录与精确正文；目录不按材料类型设资格 | 发现（依据目录）、正文读取 | 不承担执行输入；无测量工作的 TargetCommit 同样收录，引用时说明依据与边界 |
| `reasoning_evidence`（research_memory.reasoning_evidence.read） | Reasoning 冻结证据闭包的阶段上下文读取（只读，不接受新内容） | 阶段上下文 | 不承担发现（发现走六入口），不承担执行输入 |
| `implementation_content`（research_memory.implementation_content.accept/read） | Bundle 侧实现内容的接纳与执行输入绑定 | 执行输入 | 不承担研究说明或依据目录 |

阶段上下文的统一分页入口是 `research_memory.stage_context.read`（context_pack、question、literature_records、scientific_outcome、predecessor_closure、question_history、question_index、evidence_index）。跨入口正文按 RM 精确 version_ref 共用读取（`formal_results.read`、`plan_evidence.read`、stage_context），同一原件不因入口不同复制。

## 读取路径（读什么 → 用哪个入口）

- 研究问题与历史：`question_history.read`（有界页 + 稳定 ref）；关系 `question_relations.read`。
- 方法：`baselines.page/read`（含 variants 展开）。
- 数据：`datasets.page/read/register/register_version/reference/derive`。
- 环境与现实资源：`environments.page/read/register/reference`；数字说明使用返回的精确 `content.read` 引用，现实资源保留身份、来源、条件和用途。
- 文献：`research_memory.literature.page` 发现同一原文及不同问题的阅读判断；按返回 reader 阅读精确内容。阶段冻结文献仍用 `stage_context.read` source=literature_records。
- 人类输入：`human_request.read`。
- 实施与结果层次：`target_formal_results.read` / `formal_results.read`（裸 ref 反查）。
- 归类纠正：`artifact_roles.adjust`（移动当前归属并留理由，内容版本与回执不变）。

## 后续观察项

- context view 的 evidence_uses 双写与 current_targets 全量注入（2026-09-21 审计延后项）在本版未动，继续按原记录跟踪。
- 当前 Quest 内的 `literature.page` 已支持跨问题发现；阅读判断和原文保留各自的精确引用。
