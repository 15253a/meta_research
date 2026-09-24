# Reasoning 的 Owner 操作

当前 scoped 工具目录决定实际可用能力，输入已有内容不要求按固定顺序再读。按需要选择以下阶段入口及共享发现／reader：

| 操作 | 用途 |
| --- | --- |
| `advancement_engine.reasoning_stage_run.observe` | 核验当前请求、epoch 和 AE 闭包。 |
| `research_memory.reasoning_evidence.read` | 读取精确冻结证据与角色，包括已核验 Plan 复用来源。 |
| `research_graph.reasoning_context.read` | 读取已接纳 Question／Quest／Goal 与领域上下文。 |
| `research_graph.target_formal_results.read` | 按 target_ref 展开 VariantRun、适用评价和 MetricResult，包括已交接但尚未评价的 Run。 |

共享五入口及分页 reader 用于发现同 Quest 历史，不把首批目录当作全集。人类输入沿 `human_request.read` 读真实原话及材料，意见、授权、HumanRequest satisfied 与 Quest 完成确认分别核对。

## 写入权限与数据沉积

Reasoning 阶段专用写入是 `research_graph.question_relations.record`；共享 catalog 还授予 Dataset 的 register、register_version、reference、derive 及各自 reconcile。按实际研究与当前 scope 使用；执行前核实 currentness 和原件正式接纳，未知效果用原 `effect_id` 对账。数据沉积的完成条件和时序以[主 Skill 的交接前沉积步骤](../SKILL.md)为准，阶段名称不额外增加等待 ScientificOutcome 的条件。

可委派已接纳材料的有限登记任务，根最终独立读回精确版本、用途、派生关系与原件。登记复用内容保管，不代替正式执行输入绑定，也不形成新的科学测量。

## Owner 分工及恢复

AE 管阶段请求、epoch、StageCommit、后继 Cycle 和 Quest 结束；AR 管 Run／Attempt／Session／Fence、provider 操作、执行事实及恢复；RM 管不可变内容；RG 管科学接纳、QuestionAnchor、Goal 和领域事实；HC 管人类请求、Web preview 和明确完成确认。Agent 负责科学判断、唯一后继候选与修订，不签发 Owner 凭据。

provider 正常交接后，daemon 沿公开接口保存 AR 执行、RM 内容和 RG 决策，再由 AE 验证 current request／epoch 与完整 receipts 推进。各边界独立幂等；响应丢失先查询原身份，从首个未完成步骤继续。仅经公开接口核实，不读取数据库、spool、seal key 或私有状态机伪造接纳或恢复。

CandidateCompletion 还需当前 Web preview、用户明确确认、RG Goal／完成接纳和 AE 结束转换。拒绝、stale、未知或未响应都保持实际状态，不结束 Quest。
