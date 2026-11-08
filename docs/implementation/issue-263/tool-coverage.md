# #263 逐工具调用链与验证覆盖

基线 `origin/v1-test@ea421520434afcc32047ed35c937c64aa9cb9bbd`。2026-11-09 从隔离 Linux production composition 读到 **96 个唯一内置操作：47 read、24 effect、24 reconcile、1 verify**，与重开票的 96 项基线无增删。注册清单不是成功调用清单，也不是某个已获授权根的 `tools/list` 子集。

`registered-tools.json` 保留每项实际注册对象、完整 input/output schema、合同摘要 hash、handler 定位、角色目录成员关系及相关测试文件索引；测试文件索引只用于定位，不表示已执行。`collect_tools.py` 可重新生成清单，实例使用仓库确定性 provider/platform fixtures，实际 production composition/Owner 注册，未调用业务 handler、未发放 grant、未调用模型。

复现（Linux、仓库测试依赖）：`python docs/implementation/issue-263/collect_tools.py`。本次清单实际取自隔离 Linux，未执行业务 handler/模型调用或发放 grant。最终清单 snapshot `1aa8471a7500b8f79f7fec7e71a446d9921c1bb677ac71fae47e0f3a39005de5`，原始记录 `.issue-291/runs/inventory-linux-263/`；与交付源码 hash 的核对见交付验证报告；本次原清单、刷新清单均为 96 项（24 effect/24 reconcile/47 read/1 verify）。

## 调用链与证据边界

- **G：公共 MCP 边界。** 各根按公开 input schema 组参 → `/mcp` 的 live grant/fence 检查 → `SemanticMcpGateway.dispatch` → 下表 handler/Owner → MCP text 与 structuredContent → 原生 Harness 的 MCP 消费者。所有 96 项确实登记在同一 gateway，复用公共层验证只证明相同校验/包装机制，不证明 96 个 Owner 业务修复都通过。网关诊断回归见 `tests/test_semantic_failure_diagnostics.py`（实际执行结果见交付验证报告）。
- **B：宿主桥。** `deepfetch.py` 的 `_call_acquisition_operation` 内调用 `RootResidentMcpChannels.call_operation`，实际承接 `agent_runtime.acquisition.request` 及 `.reconcile`；这不是所有模型工具的统一调用桥。原 effect、批次结果、等待/恢复身份不得替换。
- **R：角色原会话。** Idea/Plan/Reasoning 通过各自 `*_skill.py`/`*_stage.py` 保有拒绝后原 native Session 的续接；Bundle 同根组织实际 Target，Target 沿 Harness/Target completion 提交和对账；Companion 用普通助手会话；Acquisition 用专用获取会话。宿主组参的 #290 和 #292 单独记录具体宿主修复；本表不把其历史缺陷重新列为当前未修，与公共网关责任分开。源码存在不等于真实模型修正成功；真实 Agent 修正与继续由 #274 的同版验收收口。
- **W：Writing。** `skills/writing-report/SKILL.md` + `references/context-and-handoff.md` → 调用方当前会话及授权 → `assistant.content.generate` → `writing_content.generate` → 原助手核对/修订/制作文件。没有独立 Writing Owner；共用 Skill 不增加 grant。网络故障注入只能证明错误信息到接口，不能冒充供应商验收。
- **T：Recorder。** `timeline_summary_provider.py` 原生摘要会话 → `RecorderChannels.grant` 的 job-scoped `timeline.sources.read` → 按原 basis 的 node/source/version 读取 → gateway 包装。该角色不在 8 种 RootAgentKind 中，但实际注册工具必须计入。只读，不创建研究效果。
- **C：创建材料特例。** `creation_work.ProtectedCreation.channel` 提供 Companion creation-materials 专属 grant（discover/read/copy/reconcile 及合同允许的 extra readers）。普通 Companion 公共目录只读，不因这里存在 copy 就普遍拥有研究写入权限。
- **H：Harness conformance。** 5 个 Owner snapshot、host_compute observe/reconcile、quest_receipt.verify 均为真实注册工具，但不属于默认研究根角色目录；由 `HarnessRuntime` conformance 的精确授权入口使用。保留登记，不假造研究 Agent 使用范围。

下表“状态”均以证据边界解释：**S**=handler/公开 schema/角色目录及包装静态核对；**P**=真实装配登记已观测；**G**=确经公共 gateway（公共测试结果从交付报告引用）。每行独有业务恢复、真实模型修正若无明确对应测试记录，仍为**未验证**，不能因 P/G 计为通过。修正指调用 Agent 修改其提交，不能修改实现、schema 或科学事实绕过拒绝。操作结果未知时保持原提交及 effect 身份先对账，不能边改 payload 边用原幂等键重投。

角色简写：D=DeepFetch，A=Acquisition，C=Companion，I=Idea，P=Plan，B=Bundle，T=Target，R=Reasoning，Rec=Recorder，H=Harness conformance；C* 为创建材料特例。目录成员身份不代替当次权限/可用性检查。

## 逐工具记录

每行给出本工具独有提交/Owner 边界、公开修正方向及原操作对账；路径均相对仓库根。精确字段约束、必填字段与对应测试索引见相同 operation 身份的 JSON 条目。

| 工具身份 | 角色 | 类型 | handler／原因来源 | 调用 Agent 可调整的内容／原操作对账 | 状态 |
|---|---|---|---|---|---|
| `advancement_engine.bundle_exhaustion.reconcile` | B | reconcile | `semantic_owner_gateway.py:_bundle_exhaustion_operations.<locals>.<lambda>` | 按 Owner feedback 修改 proposal 的真实依据；未知沿原 proposal/effect_id 对账，保留 proposal_identity/hash | P/S/G；实 Owner 提案冲突及原提案对账已验证（B1） |
| `advancement_engine.bundle_exhaustion.submit` | B | effect | `semantic_owner_gateway.py:_bundle_exhaustion_operations.<locals>.<lambda>` | 按 Owner feedback 修改 proposal 的真实依据；未知沿原 proposal/effect_id 对账，保留 proposal_identity/hash | P/S/G；实 Owner 提案冲突及原提案对账已验证（B1） |
| `advancement_engine.bundle_stage_run.observe` | B | read | `semantic_owner_gateway.py:_bundle_target_operations.<locals>.<lambda>` | 读取当前 AE request/AR scope；过期身份由原宿主重绑定，不能篡改 fence | P/S/G；独有恢复未验证 |
| `advancement_engine.reasoning_stage_run.observe` | R | read | `semantic_owner_gateway.py:_reasoning_operations.<locals>.<lambda>` | 沿当前 Reasoning request/原证据 reader 修正参数；候选拒绝回原根，特定宿主缺项见 #292 的修复记录 | P/S/G；独有恢复未验证 |
| `advancement_engine.snapshot.read` | H | read | `semantic_owner_gateway.py:_snapshot_operation.<locals>.<lambda>` | 无调用参数；只能由有效 conformance scope 读取；权限缺失不能改研究结果 | P/S/G；独有恢复未验证 |
| `agent_runtime.acquisition.request` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_root_agent_acquisition_operations.<locals>.<lambda>` | 修正 targets 的 DOI/题名线索；原 effect_id 对账已得文件/等待批次，不能重下载已得成果 | P/S/G/B；复用 B2 公共桥证据；Acquisition 独有恢复未验证 |
| `agent_runtime.acquisition.request.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_root_agent_acquisition_operations.<locals>.<lambda>` | 修正 targets 的 DOI/题名线索；原 effect_id 对账已得文件/等待批次，不能重下载已得成果 | P/S/G/B；复用 B2 公共桥分类；实际下载对账未新增验收 |
| `agent_runtime.bundle_inbox.read` | B | read | `semantic_owner_gateway.py:_bundle_target_operations.<locals>.<lambda>` | 按当前 frontier/批次边界读取；limit 合法，旧 cursor 不证明工作已完成 | P/S/G；独有恢复未验证 |
| `agent_runtime.bundle_run_binding.observe` | B | read | `semantic_owner_gateway.py:_bundle_target_operations.<locals>.<lambda>` | 读取当前 AE request/AR scope；过期身份由原宿主重绑定，不能篡改 fence | P/S/G；独有恢复未验证 |
| `agent_runtime.host_compute.observe` | H | effect | `semantic_owner_gateway.py:create_semantic_owner_gateway.<locals>.<lambda>` | 保留原 effect_id 查询主机观察；不得把未知回执视为未执行 | P/S/G；独有恢复未验证 |
| `agent_runtime.host_compute.reconcile` | H | reconcile | `semantic_owner_gateway.py:create_semantic_owner_gateway.<locals>.<lambda>` | 保留原 effect_id 查询主机观察；不得把未知回执视为未执行 | P/S/G；独有恢复未验证 |
| `agent_runtime.snapshot.read` | H | read | `semantic_owner_gateway.py:_snapshot_operation.<locals>.<lambda>` | 无调用参数；只能由有效 conformance scope 读取；权限缺失不能改研究结果 | P/S/G；独有恢复未验证 |
| `agent_runtime.target_frontier.read` | B | read | `semantic_owner_gateway.py:_bundle_target_operations.<locals>.<lambda>` | 按当前 frontier/批次边界读取；limit 合法，旧 cursor 不证明工作已完成 | P/S/G；独有恢复未验证 |
| `agent_runtime.target_run.observe` | T | read | `target_run_semantic.py:_observe_operation.<locals>.<lambda>` | 提供本根 target_ref，按当前 handle/candidate 继续；不修改 root/fence 绕过范围检查 | P/S/G；独有恢复未验证 |
| `agent_runtime.target_run.progress` | T | read | `target_run_semantic.py:_progress_operation.<locals>.read` | progress 用 cursor；raw 用 log_ref/stream_ref/before，不能混用；过期根由宿主恢复 | P/S/G；独有恢复未验证 |
| `agent_runtime.target_work.request` | B | effect | `semantic_owner_gateway.py:_target_work_operations.<locals>.<lambda>` | 按已接纳 target_ref/dispatch_decision_ref 和正式授权请求；未知保留完整原提交对账 | P/S/G；独有恢复未验证 |
| `agent_runtime.target_work.request.reconcile` | B | reconcile | `semantic_owner_gateway.py:_target_work_operations.<locals>.<lambda>` | 按已接纳 target_ref/dispatch_decision_ref 和正式授权请求；未知保留完整原提交对账 | P/S/G；独有恢复未验证 |
| `assistant.content.generate` | C/I/P/B/T/R | read | `writing_content.py:content_model_operation.<locals>.call` | 修正 config_ref/messages 的合同字段；配置/凭据/服务错误交对应负责人，原助手保留稿件继续 | P/S/G；实际 Companion grant＋受控 HTTP 诊断/配置恢复已验证（W1）；供应商未验证 |
| `deepfetch_source_action` | D | read | `deepfetch_sources.py:source_operations.<locals>.<lambda>` | 按冻结 source_id 选择 operation 及 query/url/tool_name/arguments；provided_only 不授权联网 | P/S/G；注册 gateway＋受控 scope/外部 MCP＋同 job 改参已验证（D1）；真实服务未验证 |
| `human_collaboration.snapshot.read` | H | read | `semantic_owner_gateway.py:_snapshot_operation.<locals>.<lambda>` | 无调用参数；只能由有效 conformance scope 读取；权限缺失不能改研究结果 | P/S/G；独有恢复未验证 |
| `human_guidance.feedback` | I/P/B/T/R | effect | `human_guidance_operations.py:human_guidance_operations.<locals>.feedback` | 修改真实理解、变化、继续工作和目标影响；未知用原完整 feedback/effect_id 对账 | P/S/G；独有恢复未验证 |
| `human_guidance.feedback.reconcile` | I/P/B/T/R | reconcile | `human_guidance_operations.py:human_guidance_operations.<locals>.<lambda>` | 修改真实理解、变化、继续工作和目标影响；未知用原完整 feedback/effect_id 对账 | P/S/G；独有恢复未验证 |
| `human_guidance.read` | I/P/B/T/R | effect | `human_guidance_operations.py:human_guidance_operations.<locals>.read` | 列表无需 effect；准确读取提供 delivery_ref/effect_id/分页；对账必须原 delivery/offset/limit | P/S/G；独有恢复未验证 |
| `human_guidance.read.reconcile` | I/P/B/T/R | reconcile | `human_guidance_operations.py:human_guidance_operations.<locals>.<lambda>` | 列表无需 effect；准确读取提供 delivery_ref/effect_id/分页；对账必须原 delivery/offset/limit | P/S/G；独有恢复未验证 |
| `human_request.open` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_root_agent_human_request_operations.<locals>.<lambda>` | 按请求种类写具体 condition/acceptance_conditions；缺授权向人请求；未知用原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `human_request.open.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_root_agent_human_request_operations.<locals>.<lambda>` | 按请求种类写具体 condition/acceptance_conditions；缺授权向人请求；未知用原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `human_request.read` | D/A/C/I/P/B/T/R | read | `human_research_context.py:human_research_context_operation.<locals>.<lambda>` | 读取准确 request_ref/response_ref 与原文分页；保留回应/版本和当前读取授权 | P/S/G；独有恢复未验证 |
| `research_graph.artifact_roles.adjust` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 按真实产物归属修改 role_ref/subject/reason；已有有效内容不重做；未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.artifact_roles.adjust.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 按真实产物归属修改 role_ref/subject/reason；已有有效内容不重做；未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.baselines.page` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 用准确 Baseline/Variant/Run/Protocol/结果引用与关系过滤；读取版本不等于授权执行 | P/S/G；独有恢复未验证 |
| `research_graph.baselines.read` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 用准确 Baseline/Variant/Run/Protocol/结果引用与关系过滤；读取版本不等于授权执行 | P/S/G；独有恢复未验证 |
| `research_graph.datasets.derive` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.datasets.derive.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.datasets.page` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.datasets.read` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.datasets.reference` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.datasets.reference.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.datasets.register` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.datasets.register.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.datasets.register_version` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.datasets.register_version.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.environments.page` | D/A/C/I/P/B/T/R | read | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.environments.read` | D/A/C/I/P/B/T/R | read | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.environments.reference` | D/A/I/P/B/T/R | effect | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.environments.reference.reconcile` | D/A/I/P/B/T/R | reconcile | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.environments.register` | D/A/I/P/B/T/R | effect | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.environments.register.reconcile` | D/A/I/P/B/T/R | reconcile | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_graph.formal_results.read` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 用准确 Baseline/Variant/Run/Protocol/结果引用与关系过滤；读取版本不等于授权执行 | P/S/G；独有恢复未验证 |
| `research_graph.plan_evidence.page` | P | read | `semantic_owner_gateway.py:_plan_evidence_operations.<locals>.<lambda>` | 沿完整已接纳 Plan 采用来源与精确证据 reader 修正；不把旧 coverage 当本轮义务 | P/S/G；独有恢复未验证 |
| `research_graph.quest_goal.evolve` | I/P/B/T/R | effect | `quest_goal_operations.py:quest_goal_operations.<locals>.<lambda>` | 沿当前目标/有效人类确认修正 decision；未知保留原 decision/effect_id 对账，不扩大授权 | P/S/G；独有恢复未验证 |
| `research_graph.quest_goal.evolve.reconcile` | I/P/B/T/R | reconcile | `quest_goal_operations.py:quest_goal_operations.<locals>.<lambda>` | 沿当前目标/有效人类确认修正 decision；未知保留原 decision/effect_id 对账，不扩大授权 | P/S/G；独有恢复未验证 |
| `research_graph.quest_goal.read` | I/P/B/T/R | read | `quest_goal_operations.py:quest_goal_operations.<locals>.read` | 沿当前目标/有效人类确认修正 decision；未知保留原 decision/effect_id 对账，不扩大授权 | P/S/G；独有恢复未验证 |
| `research_graph.quest_receipt.verify` | H | verify | `semantic_owner_gateway.py:create_semantic_owner_gateway.<locals>.<lambda>` | 提供原 issuer/receipt/subject/hash，回读准确回执；不得编造通过证明 | P/S/G；独有恢复未验证 |
| `research_graph.question_history.read` | D/A/C/I/P/B/T/R | read | `question_relations.py:question_history_operations.<locals>.<lambda>` | 按获授权 Question/source_quest_ref 读取；保持查询修订与返回的准确版本 | P/S/G；独有恢复未验证 |
| `research_graph.question_relations.read` | D/A/C/I/P/B/T/R | read | `question_relations.py:question_relation_operations.<locals>.<genexpr>.<lambda>` | 按获授权 Question/source_quest_ref 读取；保持查询修订与返回的准确版本 | P/S/G；独有恢复未验证 |
| `research_graph.question_relations.record` | R | effect | `question_relations.py:question_relation_operations.<locals>.<genexpr>.<lambda>` | 按真实问题关系修改两端 Question 和 notes；原 effect_id 对账；记录关系不自动切换研究问题 | P/S/G；独有恢复未验证 |
| `research_graph.question_relations.record.reconcile` | R | reconcile | `question_relations.py:question_relation_operations.<locals>.<genexpr>.<lambda>` | 按真实问题关系修改两端 Question 和 notes；原 effect_id 对账；记录关系不自动切换研究问题 | P/S/G；独有恢复未验证 |
| `research_graph.questions.page` | D/A/C/I/P/B/T/R | read | `research_content.py:research_content_operations.<locals>.discover` | 按获授权 Question/source_quest_ref 读取；保持查询修订与返回的准确版本 | P/S/G；独有恢复未验证 |
| `research_graph.reasoning_context.read` | R | read | `semantic_owner_gateway.py:_reasoning_operations.<locals>.<lambda>` | 沿当前 Reasoning request/原证据 reader 修正参数；候选拒绝回原根，特定宿主缺项见 #292 的修复记录 | P/S/G；独有恢复未验证 |
| `research_graph.snapshot.read` | H | read | `semantic_owner_gateway.py:_snapshot_operation.<locals>.<lambda>` | 无调用参数；只能由有效 conformance scope 读取；权限缺失不能改研究结果 | P/S/G；独有恢复未验证 |
| `research_graph.target_formal_results.read` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 用准确 Baseline/Variant/Run/Protocol/结果引用与关系过滤；读取版本不等于授权执行 | P/S/G；独有恢复未验证 |
| `research_graph.target_launch_request.read` | B | read | `semantic_owner_gateway.py:_bundle_target_operations.<locals>.<lambda>` | 选择已有 TargetLaunchRequest 与当前 Bundle scope；不制造目标或授权 | P/S/G；独有恢复未验证 |
| `research_memory.assets.current` | D/A/C/I/P/B/T/R | read | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S/G；独有恢复未验证 |
| `research_memory.assets.intake` | D/A/I/P/B/T/R | effect | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S/G；独有恢复未验证 |
| `research_memory.assets.intake.reconcile` | D/A/I/P/B/T/R | reconcile | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S/G；独有恢复未验证 |
| `research_memory.assets.lifecycle` | D/A/C/I/P/B/T/R | read | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S/G；独有恢复未验证 |
| `research_memory.assets.page` | D/A/C/I/P/B/T/R | read | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S/G；独有恢复未验证 |
| `research_memory.assets.retire` | D/A/I/P/B/T/R | effect | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S/G；实际引用阻碍及保留原件已验证（C2） |
| `research_memory.assets.retire.reconcile` | D/A/I/P/B/T/R | reconcile | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S/G；原完整退休命令对账已验证（C2） |
| `research_memory.content.read` | D/A/C/I/P/B/T/R | read | `research_content.py:research_content_operations.<locals>.invoke` | 使用准确 source_ref/version_ref/path 与返回的页指针；content_version_unbound 仅本次绑定失败 | P/S/G；三种分页与精确错误、保留既有内容已验证（C1/C2） |
| `research_memory.creation_basis.read` | D/A/C/I/P/B/T/R | read | `creation_basis.py:creation_basis_operations.<locals>.read` | 使用创建首包的准确 basis_ref/hash 与当前授权；不把最新资料冒充原开始基准 | P/S/G；独有恢复未验证 |
| `research_memory.implementation_content.accept` | B | effect | `semantic_owner_gateway.py:_implementation_content_operations.<locals>.<lambda>` | 保留 source/exact_version/implementation_revision/verification_evidence 的真实绑定；未知沿原完整命令对账 | P/S/G；独有恢复未验证 |
| `research_memory.implementation_content.accept.reconcile` | B | reconcile | `semantic_owner_gateway.py:_implementation_content_operations.<locals>.<lambda>` | 保留 source/exact_version/implementation_revision/verification_evidence 的真实绑定；未知沿原完整命令对账 | P/S/G；独有恢复未验证 |
| `research_memory.implementation_content.read` | B | read | `semantic_owner_gateway.py:_implementation_content_operations.<locals>.<lambda>` | 保留 source/exact_version/implementation_revision/verification_evidence 的真实绑定；未知沿原完整命令对账 | P/S/G；独有恢复未验证 |
| `research_memory.literature.page` | D/A/C/I/P/B/T/R | read | `research_content.py:research_content_operations.<locals>.discover` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_memory.literature.read` | D/A/C/I/P/B/T/R | read | `literature_operations.py:literature_operations.<locals>.read` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S/G；受控 Owner 诊断及同 Idea channel 修正已验证 |
| `research_memory.literature.reference` | D/A/I/P/B/T/R | effect | `literature_operations.py:literature_operations.<locals>.<lambda>` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_memory.literature.reference.reconcile` | D/A/I/P/B/T/R | reconcile | `literature_operations.py:literature_operations.<locals>.<lambda>` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S/G；独有恢复未验证 |
| `research_memory.literature.register` | D/A/I/P/B/T/R | effect | `literature_operations.py:literature_operations.<locals>.<lambda>` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S/G；真实注册/原 effect 对账已验证；独有业务拒绝未验证 |
| `research_memory.literature.register.reconcile` | D/A/I/P/B/T/R | reconcile | `literature_operations.py:literature_operations.<locals>.<lambda>` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S/G；真实注册/原 effect 对账已验证；独有业务拒绝未验证 |
| `research_memory.plan_evidence.read` | P | read | `semantic_owner_gateway.py:_plan_evidence_operations.<locals>.<lambda>` | 沿完整已接纳 Plan 采用来源与精确证据 reader 修正；不把旧 coverage 当本轮义务 | P/S/G；独有恢复未验证 |
| `research_memory.reasoning_evidence.read` | R | read | `semantic_owner_gateway.py:_reasoning_operations.<locals>.<lambda>` | 沿当前 Reasoning request/原证据 reader 修正参数；候选拒绝回原根，特定宿主缺项见 #292 的修复记录 | P/S/G；独有恢复未验证 |
| `research_memory.research_notes.read` | I/P/B/T/R | read | `stage_context_access.py:stage_context_operations.<locals>.<lambda>` | 读取准确阶段来源与公开分页；不得夹带 summary_only 等展示字段或改写已接纳内容 | P/S/G；Stage/Target 真实 reader 已验证（C3）；其它独有分支未验证 |
| `research_memory.snapshot.read` | H | read | `semantic_owner_gateway.py:_snapshot_operation.<locals>.<lambda>` | 无调用参数；只能由有效 conformance scope 读取；权限缺失不能改研究结果 | P/S/G；独有恢复未验证 |
| `research_memory.stage_context.read` | I/P/B/R | read | `stage_context_access.py:stage_context_operations.<locals>.<lambda>` | 读取准确阶段来源与公开分页；不得夹带 summary_only 等展示字段或改写已接纳内容 | P/S/G；独有恢复未验证 |
| `research_workspace.materials.acquire` | D/A/I/P/B/T/R | effect | `material_processing.py:material_processing_operations.<locals>.call` | 按实际已取得文件或 selection 填描述；精确内容漂移先核实，原 effect_id 对账保留有效产物 | P/S/G；独有恢复未验证 |
| `research_workspace.materials.acquire.reconcile` | D/A/I/P/B/T/R | reconcile | `material_processing.py:material_processing_operations.<locals>.call` | 按实际已取得文件或 selection 填描述；精确内容漂移先核实，原 effect_id 对账保留有效产物 | P/S/G；独有恢复未验证 |
| `research_workspace.materials.copy` | D/A/I/P/B/T/R/C* | effect | `work_materials.py:material_operations.<locals>.call` | 修正准确材料 reference/path/observation，限额按合同；原 effect_id 对账，保留已取得副本 | P/S/G；独有恢复未验证 |
| `research_workspace.materials.copy.reconcile` | D/A/I/P/B/T/R/C* | reconcile | `work_materials.py:material_operations.<locals>.call` | 修正准确材料 reference/path/observation，限额按合同；原 effect_id 对账，保留已取得副本 | P/S/G；独有恢复未验证 |
| `research_workspace.materials.discover` | D/A/C/I/P/B/T/R/C* | read | `work_materials.py:material_operations.<locals>.call` | 使用当前可见 reference/path/observation 与读页参数；普通 workspace 文件沿原生文件工具读取 | P/S/G；独有恢复未验证 |
| `research_workspace.materials.feedback` | D/A/I/P/B/T/R | effect | `material_processing.py:material_processing_operations.<locals>.call` | 按实际阅读填写理解/变化/限制/处置/selections；原 effect_id 对账，不能声称未读材料已读 | P/S/G；独有恢复未验证 |
| `research_workspace.materials.feedback.reconcile` | D/A/I/P/B/T/R | reconcile | `material_processing.py:material_processing_operations.<locals>.call` | 按实际阅读填写理解/变化/限制/处置/selections；原 effect_id 对账，不能声称未读材料已读 | P/S/G；独有恢复未验证 |
| `research_workspace.materials.read` | D/A/C/I/P/B/T/R/C* | read | `work_materials.py:material_operations.<locals>.call` | 使用当前可见 reference/path/observation 与读页参数；普通 workspace 文件沿原生文件工具读取 | P/S/G；独有恢复未验证 |
| `timeline.sources.read` | Rec | read | `recorder_sources.py:RecorderChannels._read` | 使用本次 basis 允许的 node_key/source_ref/offset；版本变化重取任务，不给旧 hash 配新正文 | P/S/G；独有恢复未验证 |
| `tools.catalog.page` | D/A/C/I/P/B/T/R | read | `semantic_mcp.py:SemanticMcpGateway._catalog_page` | 按当前授权目录/准确 operation_id 读取；分页保留 catalog_hash，变更后重新发现 | P/S/G；独有恢复未验证 |
| `tools.contracts.read` | D/A/C/I/P/B/T/R | read | `semantic_mcp.py:SemanticMcpGateway._contract_read` | 按当前授权目录/准确 operation_id 读取；分页保留 catalog_hash，变更后重新发现 | P/S/G；独有恢复未验证 |

## effect／reconcile 身份差异

24 个 effect 的公开 schema 都含 `effect_id`，但不能把所有 reconcile 都简化为只传该字段。Bundle exhaustion 需要原 `proposal`；Target work 需要原 target/dispatch 与适用人类授权字段；guidance feedback 需要原完整反馈；guidance read 的准确文档模式需要原 delivery/offset/limit；Quest goal evolve 需要原 decision；asset intake/retire 需要原完整命令和版本/引用修订；implementation_content.accept 需要原来源/版本/实施/验证及适用可选字段。其余公开 reconcile 以 effect_id 定位，继续依赖根 scope。工具返回对账入口后，调用方仍须按该入口公开 schema 用原提交组参。

`human_guidance.read` 是一个特殊混合入口：不带 delivery_ref 的列表分支不记录读取回执；带 delivery_ref 的准确读必须同时提供 stable effect_id，否则 Owner 拒绝。没有自动生成的 effect 身份。列表失败不能假称已有 effect 可对账。

## Native、外部 MCP 和共用 Skill 边界

对当前可访问的 **8771** 安装目录执行了只读检查：`meta_research_8771_v1_test/runtime/provider-tools/codex-cli/installs/0.159.0` 存在，实际 `--version` 返回 `codex-cli 0.159.0`；其 `data/external-mcp/config.json` 不存在。按照 `ExternalMcpRuntime.read_config`，这个 data root 当前显式配置的外部 MCP service 为零。没有读取或输出凭据。**这不是 #263 所指 8770 目标配置验收**，也不能推断未检查实例、原生用户配置或未来任务的外部工具为空。隔离 production composition 同样为零服务；受控外部 schema 测试是公共入口验证，不是已配置真实供应商业务验收。

已安装 CLI 的 `debug --help` 与 `app-server --help` 没有列出可直接导出完整最终原生工具目录的子命令；隔离 `debug prompt-input` 只返回 3 项消息，不返回工具 schema。没有为取得目录启动真实研究或调用模型。**最终实际 Harness/8770 原生工具逐项清单尚未实测取得**，下面按已检查的配置入口逐项保留适用/未知边界，不能视作全部原生工具通过。

| 原生能力／已见配置身份 | 实际装配及角色 | 错误和修正消费 | 证据边界 |
|---|---|---|---|
| Codex shell / `exec_command` | `root_capabilities.RootCapabilityProfile`，研究根和 Companion；provided_only 网络受限；Recorder 不授权 shell | native supervisor/stream 返回原命令状态；修正命令/工作路径，保留已有产物 | 源码映射与 CLI 安装已确认；最终原生工具身份/实际失败未验证 |
| Codex 文件变更 / `apply_patch`，原生读取 | 当前获授 workspace；共享 Skill 用原生文件工具，不新增 RW MCP 包装 | native file-change/命令结果回原会话；准确路径、现有文件和版本 | 静态；没有把角色提示词等同沙箱验证 |
| Codex 子 Agent / `spawn_agent`、`wait`、`send_message`、`followup_task`、`interrupt_agent` | root capability 观测器识别这些事件；multi_agent 由角色 profile 开启 | 原根委派/消费独立反馈；实际 API 由 Harness 提供 | 这些是源代码识别名称，不能据此声称最终工具集就这些；未实测失败 |
| 原生 web_search / web_fetch | root floor 按角色实际配置；provided_only 禁用联网 | 外部访问真实失败回原根，授权不足请求人；不重做有效获取 | 静态；供应商实际返回未验证 |
| Skill／plugin／hook | root profile 开关与原生环境能力；不是每 turn 必须使用 | 各 Skill 沿调用方授权与原会话；原生错误由底座消费 | 存在能力声明，不证明最终供应商工具名或已调用 |
| native Session fork/steer/interrupt/resume | 原始 native session 及 durable supervisor；Stage/Target 原根恢复 | 宿主保留 original operation/session 和 terminal checkpoint；未知先核对记录 | 源码和既有恢复测试定位；本表无新的真实模型验收 |
| Claude `Bash` | `harness_adapters.ClaudeHarnessAdapter --allowedTools` | CLI 自身返回；修正命令与路径 | 静态可配置 adapter；当前 FULL_CONFORMANCE_FAMILIES 仅 Codex，不宣称当前已交付启用 |
| Claude `Read` | 同上 allowedTools | 按获授权准确路径读取 | 同上 |
| Claude `Write` | 同上 allowedTools | 修正目标路径及内容，保留有效文件 | 同上 |
| Claude `Edit` | 同上 allowedTools | 核对现有文件及准确替换位置 | 同上 |
| Claude `Agent` | 同上 allowedTools | 原根接收子任务结果 | 同上 |
| Claude `WebSearch` | 同上 allowedTools | 实际网络/授权错误由原生返回 | 同上 |
| Claude `WebFetch` | 同上 allowedTools | 同上 | 同上 |
| Claude `Skill` | 同上 allowedTools | Skill 使用原会话已有能力 | 同上 |
| `mcp__meta_research__*` | Codex/Claude Harness 对本表 96 项的 runtime grant 子集 | G；禁止把前缀通配权限当 96 项都对每根可用 | 96 项 registry 完整；逐根最终 live grant 未逐一采样 |
| `mcp__meta_research_external__*` | 仅逻辑操作冻结了配置服务时提供；service.allowed_root_kinds 过滤 | external_mcp dispatch 保留服务/工具/操作/阶段、schema 和 transport 诊断 | 8771 当前显式配置为空；8770 及真实配置目录未验证 |
| Recorder `timeline.sources.read` | 唯一 job-scoped 研究数据入口，原生 CLI 读权限配置见 timeline_summary_provider | 原 basis/node/source/hash；不提供研究写入/实验/子 Agent 权限 | 本表独立计入；原生 sandbox 合规不由登记证明 |

共用 Skill 逐项静态核对：Idea、Plan、Bundle、Reasoning 的阶段 Skill 使用当前角色的 G/native 能力；Target execution 使用原生文件/命令和观察工具，正式接纳归宿主；DeepFetch 使用 `deepfetch_source_action` 及已获授来源，Acquisition 使用 nature-downloader 与原生下载能力、正式 HumanRequest；Writing 沿 W；Recorder 沿 T。nature-downloader 中的具体 publisher/API/browser 调用取决于请求、访问模式和实际可用连接，不能凭 Skill 文本凭空登记已配置外部 MCP 工具。

## 已确认诊断传播与未验证项

`OwnerConflict.details` 的生产者包括 `owners/asset_lifecycle.py:assert_asset_usable`（精确 retired version_ref）和 retirement 阻碍（reasons、active_reference_refs、active_hold_refs、observed_reference_revision）。Literature/Dataset/Environment 的精确 binding 校验会进入资产可用性检查；retirement 公共 adapter 原来已转发 details。另有 agent_runtime.py 的 target_run_admission_blocked 记录 goal_intent_ref。其它 OwnerConflict 大多只含稳定 code，保留 details 不会凭空生成业务理由。

本轮对可直接转发的 OwnerConflict 包装保留 code 与 details；刻意的授权范围粗化以及无 details 属性的 SearchSourceError/ExperimentLogError 保持原合同。受控 Owner 故障回归 `tests/test_owner_diagnostic_propagation.py`：真实 admitted Idea root/生产 Harness grant/Owner 注册文献，受控 query 抛出生产 retired-version 诊断形状，验证 text/structured 同时拿到定位，然后在同一 channel 改用准确引用读到原已接纳内容，并用原 effect 对账保留既有接纳。它证明 adapter 信息传播与同通道接口续接，**不是模型自行修正行为证明**。

已复现 red：基线 overlay 本测试在 `details` 缺失处失败（`.issue-291/runs/owner-diagnostics-red-263`）。初版该回归及普通 Literature/Bundle exhaustion 共 8 passed（`.issue-291/runs/owner-diagnostics-green2-263`）；初次 green 命令包含不存在的 test_human_guidance.py，未收集测试，已更正重跑。随后将回归加强为实际 admitted Idea + 生产 Harness grant，`owner-idea-green-263` 为 1 passed；同一最终测试在基线的对照见 `owner-idea-red-263`。每个 Owner 独有拒绝和真模型消费并未逐一实测；原生最终目录、8770 真实配置、真实供应商限流/超时后的 Agent 处置、所有角色真实模型同根修正仍需 #274 同版验收。接口探针、静态调用链、已存在测试与实际执行结果保持区分。

## 有区分力的逐工具证据索引

- **B1**：`tests/test_tool_bridge_diagnostics.py::test_bundle_conflict_preserves_reason_then_reconciles_original_proposal`，真实 Bundle Owner 接纳后，改 proposal 触发原 proposal/idempotency 原因，修正回原提交取得原接纳；不重新产生效果。文件执行结果见交付报告。
- **B2**：同文件 `test_resident_preserves_owner_rejection_and_continues_original_scope`、`test_resident_distinguishes_protocol_auth_and_unavailable`；`test_deepfetch_host_failure_diagnostics_survive_worker_and_restart` 验证宿主诊断经 worker 持久化/重启。受控故障不等于实际下载供应商验收。
- **C1**：`tests/test_tool_contract_alignment.py::test_public_content_keeps_three_distinct_pagination_endings_and_exact_errors`。
- **C2**：同文件 `test_owner_rejection_details_and_original_effect_reconciliation_survive_error_wrapper`，真实资产 retirement 被 active references 拒绝，文本/结构化保留引用定位，原完整退休命令对账 not_found，原内容仍可读。fixture 使用受控 scope，不宣称运行中的模型处置。
- **C3**：同文件 `test_stage_note_pointer_is_callable_without_stripping_display_arguments`、`test_target_note_pointer_uses_its_real_authorized_owner_handler`，实际 reader/Owner，无需去除展示字段才能调用。
- **W1**：`tests/test_companion_writing_request.py::test_configured_model_is_callable_through_the_actual_assistant_grant`，以及 `tests/test_tool_failure_diagnostics.py::test_content_model_failure_explains_the_blocker_without_exposing_provider_data`；真实 Companion grant/注册工具/gateway，外部 HTTP 由受控端点或 opener 注入。缺配置修复继续测试与 27 passed 结果见交付报告；未调用真实内容模型。
- **D1**：`tests/test_tool_failure_diagnostics.py::test_source_tool_retains_external_diagnosis_and_corrects_same_job`，`test_external_schema_failure_is_not_an_argument_repair`、`test_external_unresolved_contract_preserves_cause_through_direct_call`。使用受控 ScopeAuthority，保留原 source/job 与失败 receipt，区分提交参数与损坏 schema；不把这类探针当作全部外部工具已通过。
- **L1**：`tests/test_owner_diagnostic_propagation.py::test_literature_owner_details_survive_and_original_channel_can_continue`，最终测试真实 Idea grant；baseline red 1 failed，candidate green 1 passed，见 `owner-idea-red-263` / `owner-idea-green-263`。

上述已验证项只覆盖所写具体行为；未在索引中记录的独有业务分支、首次/修订差异以及真实 Agent 自行理解错误并修正，仍按表中未验证边界保留。所有测试记录的基线/候选版本及新增回归判断以最终验证报告为准。
