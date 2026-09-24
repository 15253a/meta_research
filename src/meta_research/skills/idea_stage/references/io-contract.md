# Idea 输入与交接

## 输入

`IdeaStageInvocation` 带精确 `stage_request_ref`、验证过的运行绑定、ContextPack ref／hash、`AcceptedQuestionBinding` 与匹配正文，以及 Quest 目标、文献、历史、证据和当前指导的绑定。默认正文给当前交接及相关索引；按需展开完整材料，索引不替代引用正文。身份、hash、schema 和 receipt 在信封中保持一致。

`AcceptedQuestionBinding` 含 Question／Quest／内容引用、内容 hash／schema、RM 内容 receipt 和 RG Question receipt；单独正文须与其精确匹配。Idea 消费科学含义，Question 的 schema 与生命周期由 Owner 管理。

当前 Evidence 首目录是发现页。需更多条目时用 `research_memory.stage_context.read` 的 `source=evidence_index` 分页，引用工具返回的精确资产版本。使用既有 `evidence_boundary.accepted_evidence_refs` 和 `NoViableCandidate.candidate_families_considered[].evidence_refs`，合计最多 100 个不同引用；按实际证据选择，不以数量作科研价值判断。RG 接纳及历史读回会核验同 Quest 角色、RM receipt 与精确内容。

## 输出与交接

提交前按[主 Skill 的独立审阅与交接](../SKILL.md)执行。Plan 只消费完整已接纳交接：IdeaOutcome ref、RM 内容 ref／receipt、RG 领域 receipt、AR 执行完成 receipt 和 AE StageCommit ref。

草稿、审阅反馈、Session 状态、拒绝、未知结果和 ExhaustionProposal 都不等于已接纳交接。Submission、Owner 接纳结果与阶段交接是不同对象；RM／RG receipts 分别核验，StageCommit 仅来自 AE。
