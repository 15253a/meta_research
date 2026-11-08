# #263 逐工具调用链与验证覆盖

基线 `origin/v1-test@ea421520434afcc32047ed35c937c64aa9cb9bbd`。2026-11-09 从隔离 Linux production composition 读到 **96 个唯一内置操作：47 read、24 effect、24 reconcile、1 verify**，与重开票的 96 项基线无增删。注册清单不是成功调用清单，也不是某个已获授权根的 `tools/list` 子集。

`registered-tools.json` 保留每项实际注册对象、完整 input/output schema、合同摘要 hash、handler 定位、角色目录成员关系及相关测试文件索引；测试文件索引只用于定位，不表示已执行。`collect_tools.py` 可重新生成清单，实例使用仓库确定性 provider/platform fixtures，实际 production composition/Owner 注册，未调用业务 handler、未发放 grant、未调用模型。

复现（Linux、仓库测试依赖）：`python docs/implementation/issue-263/collect_tools.py`。本次清单实际取自隔离 Linux，未执行业务 handler/模型调用或发放 grant。最新导出把实际归档 hash、945 个 src/tests 文件 hash 和 collector hash 记入 `registered-tools.json.metadata`；最终冻结版本以该 manifest 与交付报告核对；本次原清单、刷新清单均为 96 项（24 effect/24 reconcile/47 read/1 verify）。

## 调用链与证据边界

- **G：公共 MCP 边界。** 各根按公开 input schema 组参 → `/mcp` 的 live grant/fence 检查 → `SemanticMcpGateway.dispatch` → 下表 handler/Owner → MCP text 与 structuredContent → 原生 Harness 的 MCP 消费者。所有 96 项确实登记在同一 gateway，复用公共层验证只证明相同校验/包装机制，不证明 96 个 Owner 业务修复都通过。网关诊断回归见 `tests/test_semantic_failure_diagnostics.py`（实际执行结果见交付验证报告）。
- **B：宿主桥。** `deepfetch.py` 的 `_call_acquisition_operation` 内调用 `RootResidentMcpChannels.call_operation`，实际承接 `agent_runtime.acquisition.request` 及 `.reconcile`；这不是所有模型工具的统一调用桥。原 effect、批次结果、等待/恢复身份不得替换。
- **R：角色原会话。** Idea/Plan/Reasoning 通过各自 `*_skill.py`/`*_stage.py` 保有拒绝后原 native Session 的续接；Bundle 同根组织实际 Target，Target 沿 Harness/Target completion 提交和对账；Companion 用普通助手会话；Acquisition 用专用获取会话。宿主组参的 #290 和 #292 单独记录具体宿主修复；本表不把其历史缺陷重新列为当前未修，与公共网关责任分开。源码存在不等于真实模型修正成功；本票真实 Idea 原会话修正与继续已由 real-agent-consumption.json 验证，其他角色的组合科研 Cycle 由 #274 收口。
- **W：Writing。** `skills/writing-report/SKILL.md` + `references/context-and-handoff.md` → 调用方当前会话及授权 → `assistant.content.generate` → `writing_content.generate` → 原助手核对/修订/制作文件。没有独立 Writing Owner；共用 Skill 不增加 grant。网络故障注入只能证明错误信息到接口，不能冒充供应商验收。
- **T：Recorder。** `timeline_summary_provider.py` 原生摘要会话 → `RecorderChannels.grant` 的 job-scoped `timeline.sources.read` → 按原 basis 的 node/source/version 读取 → gateway 包装。该角色不在 8 种 RootAgentKind 中，但实际注册工具必须计入。只读，不创建研究效果。
- **C：创建材料特例。** `creation_work.ProtectedCreation.channel` 提供 Companion creation-materials 专属 grant（discover/read/copy/reconcile 及合同允许的 extra readers）。普通 Companion 公共目录只读，不因这里存在 copy 就普遍拥有研究写入权限。
- **H：Harness conformance。** 5 个 Owner snapshot、host_compute observe/reconcile、quest_receipt.verify 均为真实注册工具，但不属于默认研究根角色目录；由 `HarnessRuntime` conformance 的精确授权入口使用。保留登记，不假造研究 Agent 使用范围。

下表“状态”均以证据边界解释：**S**=handler/公开 schema/角色目录及包装静态核对；**P**=真实装配登记已观测；**G**=确经公共 gateway（公共测试结果从交付报告引用）。共享校验/包装证据按机制复用，独有变换、Owner 拒绝及原会话续接另列具体证据；没有列出的业务状态不视为已穷举，不能因 P/G 推定每个业务分支或模型行为通过。修正指调用 Agent 修改其提交，不能修改实现、schema 或科学事实绕过拒绝。操作结果未知时保持原提交及 effect 身份先对账，不能边改 payload 边用原幂等键重投。

角色简写：D=DeepFetch，A=Acquisition，C=Companion，I=Idea，P=Plan，B=Bundle，T=Target，R=Reasoning，Rec=Recorder，H=Harness conformance；C* 为创建材料特例。目录成员身份不代替当次权限/可用性检查。

## 逐工具记录

每行给出本工具独有提交/Owner 边界、公开修正方向及原操作对账；路径均相对仓库根。精确字段约束、必填字段与对应测试索引见相同 operation 身份的 JSON 条目。

| 工具身份 | 角色 | 类型 | handler／原因来源 | 调用 Agent 可调整的内容／原操作对账 | 状态 |
|---|---|---|---|---|---|
| `advancement_engine.bundle_exhaustion.reconcile` | B | reconcile | `semantic_owner_gateway.py:_bundle_exhaustion_operations.<locals>.<lambda>` | 按 Owner feedback 修改 proposal 的真实依据；未知沿原 proposal/effect_id 对账，保留 proposal_identity/hash | P/S/G；实 Owner 提案冲突及原提案对账已验证（B1） |
| `advancement_engine.bundle_exhaustion.submit` | B | effect | `semantic_owner_gateway.py:_bundle_exhaustion_operations.<locals>.<lambda>` | 按 Owner feedback 修改 proposal 的真实依据；未知沿原 proposal/effect_id 对账，保留 proposal_identity/hash | P/S/G；实 Owner 提案冲突及原提案对账已验证（B1） |
| `advancement_engine.bundle_stage_run.observe` | B | read | `semantic_owner_gateway.py:_bundle_target_operations.<locals>.<lambda>` | 读取当前 AE request/AR scope；过期身份由原宿主重绑定，不能篡改 fence | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `advancement_engine.reasoning_stage_run.observe` | R | read | `semantic_owner_gateway.py:_reasoning_operations.<locals>.<lambda>` | 沿当前 Reasoning request/原证据 reader 修正参数；候选拒绝回原根，特定宿主缺项见 #292 的修复记录 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `advancement_engine.snapshot.read` | H | read | `semantic_owner_gateway.py:_snapshot_operation.<locals>.<lambda>` | 无调用参数；只能由有效 conformance scope 读取；权限缺失不能改研究结果 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `agent_runtime.acquisition.request` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_root_agent_acquisition_operations.<locals>.<lambda>` | 修正 targets 的 DOI/题名线索；原 effect_id 对账已得文件/等待批次，不能重下载已得成果 | P/S/G/B；公共桥分类（B2）及原 session/effect 的等待、未知结果先对账已验证（A1） |
| `agent_runtime.acquisition.request.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_root_agent_acquisition_operations.<locals>.<lambda>` | 修正 targets 的 DOI/题名线索；原 effect_id 对账已得文件/等待批次，不能重下载已得成果 | P/S/G/B；lost ack/restart unknown 原 effect 对账、不重复下载已验证（A1） |
| `agent_runtime.bundle_inbox.read` | B | read | `semantic_owner_gateway.py:_bundle_target_operations.<locals>.<lambda>` | 按当前 frontier/批次边界读取；limit 合法，旧 cursor 不证明工作已完成 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `agent_runtime.bundle_run_binding.observe` | B | read | `semantic_owner_gateway.py:_bundle_target_operations.<locals>.<lambda>` | 读取当前 AE request/AR scope；过期身份由原宿主重绑定，不能篡改 fence | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `agent_runtime.host_compute.observe` | H | effect | `semantic_owner_gateway.py:create_semantic_owner_gateway.<locals>.<lambda>` | 保留原 effect_id 查询主机观察；不得把未知回执视为未执行 | P/S/G；丢回执后原 effect 对账及回放不重复实际 probe 已验证（H1） |
| `agent_runtime.host_compute.reconcile` | H | reconcile | `semantic_owner_gateway.py:create_semantic_owner_gateway.<locals>.<lambda>` | 保留原 effect_id 查询主机观察；不得把未知回执视为未执行 | P/S/G；丢回执先原 effect 对账、无重复效果已验证（H1） |
| `agent_runtime.snapshot.read` | H | read | `semantic_owner_gateway.py:_snapshot_operation.<locals>.<lambda>` | 无调用参数；只能由有效 conformance scope 读取；权限缺失不能改研究结果 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `agent_runtime.target_frontier.read` | B | read | `semantic_owner_gateway.py:_bundle_target_operations.<locals>.<lambda>` | 按当前 frontier/批次边界读取；limit 合法，旧 cursor 不证明工作已完成 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `agent_runtime.target_run.observe` | T | read | `target_run_semantic.py:_observe_operation.<locals>.<lambda>` | 提供本根 target_ref，按当前 handle/candidate 继续；不修改 root/fence 绕过范围检查 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `agent_runtime.target_run.progress` | T | read | `target_run_semantic.py:_progress_operation.<locals>.read` | progress 用 cursor；raw 用 log_ref/stream_ref/before，不能混用；过期根由宿主恢复 | P/S/G；真实 Target grant 的五种模式条件拒绝及同通道修正（S1） |
| `agent_runtime.target_work.request` | B | effect | `semantic_owner_gateway.py:_target_work_operations.<locals>.<lambda>` | 按已接纳 target_ref/dispatch_decision_ref 和正式授权请求；未知保留完整原提交对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `agent_runtime.target_work.request.reconcile` | B | reconcile | `semantic_owner_gateway.py:_target_work_operations.<locals>.<lambda>` | 按已接纳 target_ref/dispatch_decision_ref 和正式授权请求；未知保留完整原提交对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `assistant.content.generate` | C/I/P/B/T/R | read | `writing_content.py:content_model_operation.<locals>.call` | 修正 config_ref/messages 的合同字段；配置/凭据/服务错误交对应负责人，原助手保留稿件继续 | P/S/G；实际 Companion grant＋受控 HTTP 诊断/配置恢复已验证（W1）；供应商未验证 |
| `deepfetch_source_action` | D | read | `deepfetch_sources.py:source_operations.<locals>.<lambda>` | 按冻结 source_id 选择 operation 及 query/url/tool_name/arguments；provided_only 不授权联网 | P/S/G；注册 gateway＋受控 scope/外部 MCP＋同 job 改参已验证（D1）；真实服务未验证 |
| `human_collaboration.snapshot.read` | H | read | `semantic_owner_gateway.py:_snapshot_operation.<locals>.<lambda>` | 无调用参数；只能由有效 conformance scope 读取；权限缺失不能改研究结果 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `human_guidance.feedback` | I/P/B/T/R | effect | `human_guidance_operations.py:human_guidance_operations.<locals>.feedback` | 修改真实理解、变化、继续工作和目标影响；未知用原完整 feedback/effect_id 对账 | P/S/G；原 frozen delivery 反馈、审查 child 禁写及精确旧版绑定已有通过节点（U1） |
| `human_guidance.feedback.reconcile` | I/P/B/T/R | reconcile | `human_guidance_operations.py:human_guidance_operations.<locals>.<lambda>` | 修改真实理解、变化、继续工作和目标影响；未知用原完整 feedback/effect_id 对账 | P/S；共享包装/原 effect 授权复用（G/U1），未把目录可见性当作所有对账分支通过 |
| `human_guidance.read` | I/P/B/T/R | effect | `human_guidance_operations.py:human_guidance_operations.<locals>.read` | 列表无需 effect；准确读取提供 delivery_ref/effect_id/分页；对账必须原 delivery/offset/limit | P/S/G；真实 Idea grant 缺字段修正后继续原 delivery（U1/S1） |
| `human_guidance.read.reconcile` | I/P/B/T/R | reconcile | `human_guidance_operations.py:human_guidance_operations.<locals>.<lambda>` | 列表无需 effect；准确读取提供 delivery_ref/effect_id/分页；对账必须原 delivery/offset/limit | P/S/G；同 cut/delivery/原 effect 的相同 read 回执（U1/S1） |
| `human_request.open` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_root_agent_human_request_operations.<locals>.<lambda>` | 按请求种类写具体 condition/acceptance_conditions；缺授权向人请求；未知用原 effect_id 对账 | P/S/G；真实条件拒绝后原 effect 修正并接纳，前置效果状态准确（S1） |
| `human_request.open.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_root_agent_human_request_operations.<locals>.<lambda>` | 按请求种类写具体 condition/acceptance_conditions；缺授权向人请求；未知用原 effect_id 对账 | P/S/G；共用条件检查和原完整提交定位复用 S1，状态不假称未记录 |
| `human_request.read` | D/A/C/I/P/B/T/R | read | `human_research_context.py:human_research_context_operation.<locals>.<lambda>` | 读取准确 request_ref/response_ref 与原文分页；保留回应/版本和当前读取授权 | P/S/G；真实选择器、guidance/input 公开分页字段及原 revision 续读（P1/S1） |
| `research_graph.artifact_roles.adjust` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 按真实产物归属修改 role_ref/subject/reason；已有有效内容不重做；未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.artifact_roles.adjust.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 按真实产物归属修改 role_ref/subject/reason；已有有效内容不重做；未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.baselines.page` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 用准确 Baseline/Variant/Run/Protocol/结果引用与关系过滤；读取版本不等于授权执行 | P/S/G；真实查询 revision 拒绝与纠正已验证（F1） |
| `research_graph.baselines.read` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 用准确 Baseline/Variant/Run/Protocol/结果引用与关系过滤；读取版本不等于授权执行 | P/S/G；真实深引用缺 relation 拒绝与同授权纠正已验证（F1） |
| `research_graph.datasets.derive` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.datasets.derive.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.datasets.page` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；真实 filter/查询 revision 拒绝与纠正已验证（F1） |
| `research_graph.datasets.read` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；真实引用条件拒绝与受控 Owner 存储故障传播/恢复已验证（F1） |
| `research_graph.datasets.reference` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.datasets.reference.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.datasets.register` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；真实登记及保留原 effect 已验证（F1）；独有登记拒绝未验证 |
| `research_graph.datasets.register.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S/G；受控读取故障后原登记 effect 对账已验证（F1） |
| `research_graph.datasets.register_version` | D/A/I/P/B/T/R | effect | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.datasets.register_version.reconcile` | D/A/I/P/B/T/R | reconcile | `semantic_owner_gateway.py:_dataset_operations.<locals>.<lambda>` | 按真实 Dataset/精确 DatasetVersion/Question 及已接纳 binding 修改；查询保持 revision，未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.environments.page` | D/A/C/I/P/B/T/R | read | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S/G；真实 filter/查询 revision 拒绝与纠正已验证（F1） |
| `research_graph.environments.read` | D/A/C/I/P/B/T/R | read | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S/G；真实引用互斥拒绝、同授权纠正已验证（E1） |
| `research_graph.environments.reference` | D/A/I/P/B/T/R | effect | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.environments.reference.reconcile` | D/A/I/P/B/T/R | reconcile | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.environments.register` | D/A/I/P/B/T/R | effect | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S/G；真实登记及原 effect 对账已验证（E1）；独有登记拒绝未验证 |
| `research_graph.environments.register.reconcile` | D/A/I/P/B/T/R | reconcile | `environment_operations.py:environment_operations.<locals>.<lambda>` | 按真实资源来源/精确 Environment 与用途修正；read 恰选一种引用；查询保持 revision，未知原 effect_id 对账 | P/S/G；原登记 effect 对账保留已验证（E1） |
| `research_graph.formal_results.read` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 用准确 Baseline/Variant/Run/Protocol/结果引用与关系过滤；读取版本不等于授权执行 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.plan_evidence.page` | P | read | `semantic_owner_gateway.py:_plan_evidence_operations.<locals>.<lambda>` | 沿完整已接纳 Plan 采用来源与精确证据 reader 修正；不把旧 coverage 当本轮义务 | P/S/G；真实 Plan grant 与精确 evidence，受控 Owner 故障恢复后原绑定不变（O1） |
| `research_graph.quest_goal.evolve` | I/P/B/T/R | effect | `quest_goal_operations.py:quest_goal_operations.<locals>.<lambda>` | 沿当前目标/有效人类确认修正 decision；未知保留原 decision/effect_id 对账，不扩大授权 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.quest_goal.evolve.reconcile` | I/P/B/T/R | reconcile | `quest_goal_operations.py:quest_goal_operations.<locals>.<lambda>` | 沿当前目标/有效人类确认修正 decision；未知保留原 decision/effect_id 对账，不扩大授权 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.quest_goal.read` | I/P/B/T/R | read | `quest_goal_operations.py:quest_goal_operations.<locals>.read` | 沿当前目标/有效人类确认修正 decision；未知保留原 decision/effect_id 对账，不扩大授权 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.quest_receipt.verify` | H | verify | `semantic_owner_gateway.py:create_semantic_owner_gateway.<locals>.<lambda>` | 提供原 issuer/receipt/subject/hash，回读准确回执；不得编造通过证明 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.question_history.read` | D/A/C/I/P/B/T/R | read | `question_relations.py:question_history_operations.<locals>.<lambda>` | 按获授权 Question/source_quest_ref 读取；保持查询修订与返回的准确版本 | P/S/G；实际 reader 旧 revision 拒绝后同 grant 纠正已验证（P1） |
| `research_graph.question_relations.read` | D/A/C/I/P/B/T/R | read | `question_relations.py:question_relation_operations.<locals>.<genexpr>.<lambda>` | 按获授权 Question/source_quest_ref 读取；保持查询修订与返回的准确版本 | P/S/G；实际 reader 旧 revision 拒绝后同 grant 纠正已验证（P1） |
| `research_graph.question_relations.record` | R | effect | `question_relations.py:question_relation_operations.<locals>.<genexpr>.<lambda>` | 按真实问题关系修改两端 Question 和 notes；原 effect_id 对账；记录关系不自动切换研究问题 | P/S/G；真实自关系拒绝、对账后改用既有另一 Question 已验证（Q1） |
| `research_graph.question_relations.record.reconcile` | R | reconcile | `question_relations.py:question_relation_operations.<locals>.<genexpr>.<lambda>` | 按真实问题关系修改两端 Question 和 notes；原 effect_id 对账；记录关系不自动切换研究问题 | P/S/G；拒绝后 not_found、原 effect 合法接纳及对账已验证（Q1） |
| `research_graph.questions.page` | D/A/C/I/P/B/T/R | read | `research_content.py:research_content_operations.<locals>.discover` | 按获授权 Question/source_quest_ref 读取；保持查询修订与返回的准确版本 | P/S/G；真实查询 revision 拒绝与纠正已验证（F1） |
| `research_graph.reasoning_context.read` | R | read | `semantic_owner_gateway.py:_reasoning_operations.<locals>.<lambda>` | 沿当前 Reasoning request/原证据 reader 修正参数；候选拒绝回原根，特定宿主缺项见 #292 的修复记录 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.snapshot.read` | H | read | `semantic_owner_gateway.py:_snapshot_operation.<locals>.<lambda>` | 无调用参数；只能由有效 conformance scope 读取；权限缺失不能改研究结果 | P/S/G；真实 typed Harness 授权读取及跨 scope 拒绝已验证（H1） |
| `research_graph.target_formal_results.read` | D/A/C/I/P/B/T/R | read | `semantic_owner_gateway.py:_baseline_operations.<locals>.<lambda>` | 用准确 Baseline/Variant/Run/Protocol/结果引用与关系过滤；读取版本不等于授权执行 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_graph.target_launch_request.read` | B | read | `semantic_owner_gateway.py:_bundle_target_operations.<locals>.<lambda>` | 选择已有 TargetLaunchRequest 与当前 Bundle scope；不制造目标或授权 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_memory.assets.current` | D/A/C/I/P/B/T/R | read | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_memory.assets.intake` | D/A/I/P/B/T/R | effect | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S/G；真实接纳后同 effect 畸形提交保持 unknown，原对账及正文保留（S1） |
| `research_memory.assets.intake.reconcile` | D/A/I/P/B/T/R | reconcile | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S/G；原完整 intake 对账取得相同接纳回执及内容（S1） |
| `research_memory.assets.lifecycle` | D/A/C/I/P/B/T/R | read | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_memory.assets.page` | D/A/C/I/P/B/T/R | read | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_memory.assets.retire` | D/A/I/P/B/T/R | effect | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S/G；实际引用阻碍及保留原件已验证（C2） |
| `research_memory.assets.retire.reconcile` | D/A/I/P/B/T/R | reconcile | `asset_lifecycle_operations.py:asset_lifecycle_operations.<locals>.<lambda>` | 按实际 custody/source 与 change/expected_revision/引用修订修正；retired 版本不替换科学事实；未知用原完整命令对账 | P/S/G；原完整退休命令对账已验证（C2） |
| `research_memory.content.read` | D/A/C/I/P/B/T/R | read | `research_content.py:research_content_operations.<locals>.invoke` | 使用准确 source_ref/version_ref/path 与返回的页指针；content_version_unbound 仅本次绑定失败 | P/S/G；三种分页与精确错误、保留既有内容已验证（C1/C2） |
| `research_memory.creation_basis.read` | D/A/C/I/P/B/T/R | read | `creation_basis.py:creation_basis_operations.<locals>.read` | 使用创建首包的准确 basis_ref/hash 与当前授权；不把最新资料冒充原开始基准 | P/S/G；真实 protected grant 缺 selector 分类、同 basis 保留及 offset 诊断已验证（C4） |
| `research_memory.implementation_content.accept` | B | effect | `semantic_owner_gateway.py:_implementation_content_operations.<locals>.<lambda>` | 保留 source/exact_version/implementation_revision/verification_evidence 的真实绑定；未知沿原完整命令对账 | P/S/G；真实 Bundle 已接纳后内容冲突、原提交对账保留已验证（O1） |
| `research_memory.implementation_content.accept.reconcile` | B | reconcile | `semantic_owner_gateway.py:_implementation_content_operations.<locals>.<lambda>` | 保留 source/exact_version/implementation_revision/verification_evidence 的真实绑定；未知沿原完整命令对账 | P/S/G；真实原命令对账冲突定位，受控 Owner I/O 透传/恢复已验证（O1） |
| `research_memory.implementation_content.read` | B | read | `semantic_owner_gateway.py:_implementation_content_operations.<locals>.<lambda>` | 保留 source/exact_version/implementation_revision/verification_evidence 的真实绑定；未知沿原完整命令对账 | P/S/G；真实已接纳版本保留；受控 Owner I/O 透传/恢复已验证（O1） |
| `research_memory.literature.page` | D/A/C/I/P/B/T/R | read | `research_content.py:research_content_operations.<locals>.discover` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S/G；真实查询 revision 拒绝与纠正已验证（F1） |
| `research_memory.literature.read` | D/A/C/I/P/B/T/R | read | `literature_operations.py:literature_operations.<locals>.read` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S/G；受控 Owner 诊断及同 Idea channel 修正已验证 |
| `research_memory.literature.reference` | D/A/I/P/B/T/R | effect | `literature_operations.py:literature_operations.<locals>.<lambda>` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_memory.literature.reference.reconcile` | D/A/I/P/B/T/R | reconcile | `literature_operations.py:literature_operations.<locals>.<lambda>` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_memory.literature.register` | D/A/I/P/B/T/R | effect | `literature_operations.py:literature_operations.<locals>.<lambda>` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S/G；真实全文 binding 拒绝、维持实际 missing 状态后接纳已验证（F1） |
| `research_memory.literature.register.reconcile` | D/A/I/P/B/T/R | reconcile | `literature_operations.py:literature_operations.<locals>.<lambda>` | 修正外部来源、真实全文状态及已接纳原件/读后材料 binding；查询保持 revision，未知原 effect_id 对账 | P/S/G；错误全文提交先原 effect 对账无接纳已验证（F1） |
| `research_memory.plan_evidence.read` | P | read | `semantic_owner_gateway.py:_plan_evidence_operations.<locals>.<lambda>` | 沿完整已接纳 Plan 采用来源与精确证据 reader 修正；不把旧 coverage 当本轮义务 | P/S/G；真实 Plan 精确 version/hash/role 原文，受控 Owner 故障恢复后不变（O1） |
| `research_memory.reasoning_evidence.read` | R | read | `semantic_owner_gateway.py:_reasoning_operations.<locals>.<lambda>` | 沿当前 Reasoning request/原证据 reader 修正参数；候选拒绝回原根，特定宿主缺项见 #292 的修复记录 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_memory.research_notes.read` | I/P/B/T/R | read | `stage_context_access.py:stage_context_operations.<locals>.<lambda>` | 读取准确阶段来源与公开分页；不得夹带 summary_only 等展示字段或改写已接纳内容 | P/S/G；Stage/Target 真实 reader 已验证（C3）；其它独有分支未验证 |
| `research_memory.snapshot.read` | H | read | `semantic_owner_gateway.py:_snapshot_operation.<locals>.<lambda>` | 无调用参数；只能由有效 conformance scope 读取；权限缺失不能改研究结果 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_memory.stage_context.read` | I/P/B/R | read | `stage_context_access.py:stage_context_operations.<locals>.<lambda>` | 读取准确阶段来源与公开分页；不得夹带 summary_only 等展示字段或改写已接纳内容 | P/S/G；实际索引/字节分页及 path、UTF-8 错误纠正已验证（P1/C4） |
| `research_workspace.materials.acquire` | D/A/I/P/B/T/R | effect | `material_processing.py:material_processing_operations.<locals>.call` | 按实际已取得文件或 selection 填描述；精确内容漂移先核实，原 effect_id 对账保留有效产物 | P/S/G；真实来源互斥拒绝，原效果 absent 后修正成功（S1） |
| `research_workspace.materials.acquire.reconcile` | D/A/I/P/B/T/R | reconcile | `material_processing.py:material_processing_operations.<locals>.call` | 按实际已取得文件或 selection 填描述；精确内容漂移先核实，原 effect_id 对账保留有效产物 | P/S/G；拒绝前后的原 effect absent/accepted 对账（S1） |
| `research_workspace.materials.copy` | D/A/I/P/B/T/R/C* | effect | `work_materials.py:material_operations.<locals>.call` | 修正准确材料 reference/path/observation，限额按合同；原 effect_id 对账，保留已取得副本 | P/S/G；真实 protected copy 限额拒绝、对账后合法复制且保留已编辑副本（M1） |
| `research_workspace.materials.copy.reconcile` | D/A/I/P/B/T/R/C* | reconcile | `work_materials.py:material_operations.<locals>.call` | 修正准确材料 reference/path/observation，限额按合同；原 effect_id 对账，保留已取得副本 | P/S/G；真实 protected copy 失败/既有接纳对账和冲突保留已验证（M1） |
| `research_workspace.materials.discover` | D/A/C/I/P/B/T/R/C* | read | `work_materials.py:material_operations.<locals>.call` | 使用当前可见 reference/path/observation 与读页参数；普通 workspace 文件沿原生文件工具读取 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_workspace.materials.feedback` | D/A/I/P/B/T/R | effect | `material_processing.py:material_processing_operations.<locals>.call` | 按实际阅读填写理解/变化/限制/处置/selections；原 effect_id 对账，不能声称未读材料已读 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_workspace.materials.feedback.reconcile` | D/A/I/P/B/T/R | reconcile | `material_processing.py:material_processing_operations.<locals>.call` | 按实际阅读填写理解/变化/限制/处置/selections；原 effect_id 对账，不能声称未读材料已读 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `research_workspace.materials.read` | D/A/C/I/P/B/T/R/C* | read | `work_materials.py:material_operations.<locals>.call` | 使用当前可见 reference/path/observation 与读页参数；普通 workspace 文件沿原生文件工具读取 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `timeline.sources.read` | Rec | read | `recorder_sources.py:RecorderChannels._read` | 使用本次 basis 允许的 node_key/source_ref/offset；版本变化重取任务，不给旧 hash 配新正文 | P/S/G；真实 job 越界拒绝/续页及受控 Owner 故障透传已验证（T1） |
| `tools.catalog.page` | D/A/C/I/P/B/T/R | read | `semantic_mcp.py:SemanticMcpGateway._catalog_page` | 按当前授权目录/准确 operation_id 读取；分页保留 catalog_hash，变更后重新发现 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |
| `tools.contracts.read` | D/A/C/I/P/B/T/R | read | `semantic_mcp.py:SemanticMcpGateway._contract_read` | 按当前授权目录/准确 operation_id 读取；分页保留 catalog_hash，变更后重新发现 | P/S；共享校验/包装复用（G）；本票未新增恢复控制，Owner 业务状态未穷举 |

## effect／reconcile 身份差异

24 个 effect 的公开 schema 都含 `effect_id`，但不能把所有 reconcile 都简化为只传该字段。Bundle exhaustion 需要原 `proposal`；Target work 需要原 target/dispatch 与适用人类授权字段；guidance feedback 需要原完整反馈；guidance read 的准确文档模式需要原 delivery/offset/limit；Quest goal evolve 需要原 decision；asset intake/retire 需要原完整命令和版本/引用修订；implementation_content.accept 需要原来源/版本/实施/验证及适用可选字段。其余公开 reconcile 以 effect_id 定位，继续依赖根 scope。工具返回对账入口后，调用方仍须按该入口公开 schema 用原提交组参。

`human_guidance.read` 是一个特殊混合入口：不带 delivery_ref 的列表分支不记录读取回执；带 delivery_ref 的准确读必须同时提供 stable effect_id，否则 Owner 拒绝。没有自动生成的 effect 身份。列表失败不能假称已有 effect 可对账。

## Native、外部 MCP 和共用 Skill 边界

2026-11-09 00:43（Asia/Shanghai）沿部署记录定位并实际只读检查 **8770**：`/mnt/wl2605001/projects/meta-research-8770-v1-test`。正在运行的 pid 3681139 是 `meta_research.daemon`，命令的 data root 与 cwd 均匹配该部署，端口 8770。磁盘源码为 `a74e91fbeb848f744714f8826f13acdc7c0d29ba`，有 23 个已有 dirty files，全部保持不变；它不是本次基于 ea42152 的修复交付版本。安全元数据和关键源码 hash 见 [target-8770-inventory.json](target-8770-inventory.json)。

**目标实例当前外部配置已核对：** `data/external-mcp/config.json` 不存在，按 `ExternalMcpRuntime.read_config` 为零显式服务；operation snapshots 为 0。`data/search-sources/registry.sqlite3` 使用 `mode=ro` 读取元数据和计数，versions、heads、selections、manifests、jobs、receipts 等均为 0，没有已配置 supplemental API/website/MCP 来源工具身份。没有读取凭据文件或输出连接内容，也没有配置新服务。这个结论来自实际 8770，不再用 8771 代替；不推广到未来配置或其它实例。受控外部 schema 测试仍是公共入口验证，不能称为真实供应商验收。

**候选原生目录已有实际装配证据。** [native-tools.json](native-tools.json) 绑定候选 `da890f4` 与实际安装的 Codex CLI 0.159.0；后续诊断修改未改变原生 argv 入口；[native-source-reuse.json](native-source-reuse.json) 核对 11 个完整 adapter/策略模块的 LF hash，以及 Recorder operation 的 AST，均与实际捕获版本相同。`collect_native_tools.py` 使用真实 adapter 参数入口，13 变体包括 8 个根、Writing 的 Companion 入口、Recorder、Target 两种策略和 DeepFetch/Acquisition 的 provided_only。12 个根/Writing变体各捕获 **22 个初始原生可调用身份**；Recorder 为 **3 个**。gpt-6.1-sol 为 code-mode，实际工具位于出站 `input[type=additional_tools].tools`，还需展开 `functions.exec` 内真实 `declare const tools` 声明；完整声明保留 source_pointer。text/image/store 等 runtime helper 不计作工具。早期仅看顶层 tools、缺模型元数据的 9/0 结果已作废。

装配使用本地 Responses 拒绝端点与空 MCP fixture，CLI、adapter和模型目录是真实对象；这是原生装配证据，不是 live semantic grant 或模型执行证明。各角色内置操作子集仍由前表 G/角色目录及真实 grant 验证约束。Recorder 使用其实际 restricted catalog；3 个资源 reader 不授予任意研究数据读取权，研究数据仍仅走 job-scoped `timeline.sources.read`。

**延迟工具由真实消费者另行核对。** Companion 在原会话读取 ALL_TOOLS，实际为 12 个嵌套工具，比初始 11 项多 `tools.web__run`，合并直接声明后为 **23 个**。DeepFetch 和 Acquisition 的 provided_only 各用真实会话读取目录，均为 11 个嵌套工具、无 web__run，与初始 22 项一致；未执行目录内任何工具，保留 workspace-write/禁网配置。其它普通根的初始声明和 live-web 配置一致，复用延迟 web 结论是共享配置证据，不声称逐根再跑模型。跨角色共有 **26 个带前缀调用身份**（23 个普通根、3 个 Recorder）；Recorder 的后三项是同类资源能力的不同包装。

**目标 Harness 范围。** 8770 daemon 调用默认 build_production_runtime，默认 composition 只装配 CodexHarnessAdapter，FULL_CONFORMANCE_FAMILIES=(codex,)；只读 ar_harness_runs/ar_target_harness_admissions 均为 0。产品默认模型为 gpt-6.1-sol；源码允许其它模型不等于本次选择。Claude 是可选源码 adapter，Gemini 无此 production adapter；独立审查使用的 CLI 不代表产品启用。13 变体覆盖本候选实际 Codex 角色入口和影响能力的策略，不包含未配置的 Harness、插件或未来服务。

下表 **NC** 指实际目录/声明；**N1** 指 [native-recovery-summary.json](native-recovery-summary.json) 和 [native-recovery-tool-events.json](native-recovery-tool-events.json)。真实模型/CLI/Companion adapter 在原根 `01a1bc3d-e23c-73c0-812f-cf47873644e9` 连续两轮成功：缺失文件退出1→列目录/读准确输入；错误补丁上下文→回读/仅改状态行；无效child名字→真实spawn/wait；第二轮原session resume、原child `/root/read_fixture_263` followup/wait再核验，已有文件hash不变、未造缺失文件。真实native日志保留具体失败文本；提交证据移除无关内部元数据并将加密子任务消息替换为hash。N1是有意触发错误的真实消费者验收，不代表全部原生错误分支。

**共享路径**表示相同原生返回通道经过N1，该工具自身失败分支未单独实测。命令、文件变更、child/provider结果回原 native Session，产品 native_codex_bridge/supervisor 保留状态与身份；未知先检查原session/命令/child，不新造身份重做有效工作。

| 实际原生身份 | 适用角色/调用链 | 修正方向与保留身份 | 证据 |
|---|---|---|---|
| `functions.exec` | 12根/Writing变体 code-mode入口 | 修正JS/嵌套参数，核对真实结果；未知动作先对账 | NC；N1错误回传与继续 |
| `functions.wait` | 同上，yielded exec cell | 使用原cell_id，取回状态而非重发动作 | NC；共享路径 |
| `tools.exec_command` | 同上，native command | 核对stderr/exit/路径；进程未终止时保留session_id | NC；N1缺失文件exit1后修正 |
| `tools.write_stdin` | 同上，原command session | 用返回的session_id先poll，不能把未知当未执行 | NC；共享路径 |
| `tools.apply_patch` | 同上，native file change | 回读准确文件/上下文，只改目标片段 | NC；N1上下文错误后修正 |
| `tools.view_image` | 同上，本地图片reader | 纠正获授权路径/格式，读取实际文件 | NC；共享路径 |
| `tools.list_mcp_resources` | 普通/受限根 code-mode | 核对实际server/cursor；无资源不是读取成功 | NC；共享路径 |
| `tools.list_mcp_resource_templates` | 同上 | 先取实际模板再按其参数读取，保留server | NC；共享路径 |
| `tools.read_mcp_resource` | 同上 | 使用已发现server/URI及当前权限 | NC；共享路径 |
| `functions.list_mcp_resources` | Recorder直接函数 | 资源发现不增加研究grant | Recorder NC；自身错误未单测 |
| `functions.list_mcp_resource_templates` | Recorder直接函数 | 模板发现仍受job grant约束 | Recorder NC；自身错误未单测 |
| `functions.read_mcp_resource` | Recorder直接函数 | 不替代timeline来源版本合同 | Recorder NC；自身错误未单测 |
| `tools.get_goal` | 普通/受限根 | 读原goal状态，不假造不存在的goal | NC；共享路径 |
| `tools.create_goal` | 同上，仍需用户明确请求 | 修正目标/预算；已有goal先读 | NC；共享路径，未创建goal |
| `tools.update_goal` | 同上 | 核对状态及暂停/完成条件，保留goal身份 | NC；共享路径，未改goal |
| `tools.clock__curr_time` | 同上 | 读取实际时间；不可用时报告 | NC；共享路径 |
| `clock.sleep` | 同上 | 修正时长，保留可被新输入中断的会话 | NC；共享路径 |
| `functions.request_user_input` | 同上，native模式限制 | 按允许模式问必要缺项，不假定授权到达 | NC；共享路径 |
| `functions.request_user_input_async` | 同上 | 保留原请求/答案关联，缺答案不推定许可 | NC；共享路径 |
| `collaboration.spawn_agent` | 同上，真实child创建 | 修正task_name/参数，保存返回child身份 | NC；N1真实spawn并独立读取 |
| `collaboration.send_message` | 同上，已有child | 使用真实身份；格式/不存在错误回原根 | NC；N1名称格式错误 |
| `collaboration.followup_task` | 同上，原child继续 | 复用child，不用新child掩盖未知结果 | NC；N1第二轮同child继续 |
| `collaboration.wait_agent` | 同上 | 等原child真实结果，超时不声称完成 | NC；N1两轮实际等待 |
| `collaboration.list_agents` | 同上 | 查已存在child状态后定位 | NC；共享路径 |
| `collaboration.interrupt_agent` | 同上 | 核对child与状态，保留有效产物 | NC；共享路径，未执行中断 |
| `tools.web__run` | 普通根live-web；provided_only/Recorder无 | 按源/参数/网络原因修正，保留有效结果；未知不盲重做 | Companion真实ALL_TOOLS；普通根共享配置；两provided_only实测无此工具；未调用供应商 |

原生Session start/resume/fork/steer/interrupt是底座生命周期方法，不虚构成额外模型工具。N1实测同session resume和同child followup，其它分支复用既有恢复测试。Skill/plugin/hook是配置表面，仅在实际registry出现名字时计数；本次真实ALL_TOOLS未出现额外插件工具，目标外部配置仍为空。临时凭据/反向隧道已清理，见 native-credential-cleanup.json；伪凭据初始化失败也验证finally清理，见 native-credential-cleanup-negative.json。


共用 Skill 逐项静态核对：Idea、Plan、Bundle、Reasoning 的阶段 Skill 使用当前角色的 G/native 能力；Target execution 使用原生文件/命令和观察工具，正式接纳归宿主；DeepFetch 使用 `deepfetch_source_action` 及已获授来源，Acquisition 使用 nature-downloader 与原生下载能力、正式 HumanRequest；Writing 沿 W；Recorder 沿 T。nature-downloader 中的具体 publisher/API/browser 调用取决于请求、访问模式和实际可用连接，不能凭 Skill 文本凭空登记已配置外部 MCP 工具。

## 已确认诊断传播与未验证项

`OwnerConflict.details` 的生产者包括 `owners/asset_lifecycle.py:assert_asset_usable`（精确 retired version_ref）和 retirement 阻碍（reasons、active_reference_refs、active_hold_refs、observed_reference_revision）。Literature/Dataset/Environment 的精确 binding 校验会进入资产可用性检查；retirement 公共 adapter 原来已转发 details。另有 agent_runtime.py 的 target_run_admission_blocked 记录 goal_intent_ref。其它 OwnerConflict 大多只含稳定 code，保留 details 不会凭空生成业务理由。

本轮对可直接转发的 OwnerConflict 包装保留 code 与 details；刻意的授权范围粗化以及无 details 属性的 SearchSourceError/ExperimentLogError 保持原合同。受控 Owner 故障回归 `tests/test_owner_diagnostic_propagation.py`：真实 admitted Idea root/生产 Harness grant/Owner 注册文献，受控 query 抛出生产 retired-version 诊断形状，验证 text/structured 同时拿到定位，然后在同一 channel 改用准确引用读到原已接纳内容，并用原 effect 对账保留既有接纳。它证明 adapter 信息传播与同通道接口续接，**不是模型自行修正行为证明**。

已复现 red：基线 overlay 本测试在 `details` 缺失处失败（`.issue-291/runs/owner-diagnostics-red-263`）。初版该回归及普通 Literature/Bundle exhaustion 共 8 passed（`.issue-291/runs/owner-diagnostics-green2-263`）；初次 green 命令包含不存在的 test_human_guidance.py，未收集测试，已更正重跑。随后将回归加强为实际 admitted Idea + 生产 Harness grant，`owner-idea-green-263` 为 1 passed；同一最终测试在基线的对照见 `owner-idea-red-263`。本轮原生目录及代表性同根纠错见 NC/N1，真实 Idea reader/effect 消费见 real-agent-consumption.json，各角色链见 role-chain-validation.md。每个 Owner 独有拒绝未做故障笛卡尔验证；修复版部署后的 8770 配置、真实供应商限流/超时处置及其它角色端到端真实研究运行属于 #274 同版验收范围。接口探针、静态调用链、已存在测试与真实消费者证据保持区分。

## 有区分力的逐工具证据索引

- **B1**：`tests/test_tool_bridge_diagnostics.py::test_bundle_conflict_preserves_reason_then_reconciles_original_proposal`，真实 Bundle Owner 接纳后，改 proposal 触发原 proposal/idempotency 原因，修正回原提交取得原接纳；不重新产生效果。文件执行结果见交付报告。
- **B2**：同文件 `test_resident_preserves_owner_rejection_and_continues_original_scope`、`test_resident_distinguishes_protocol_auth_and_unavailable`；`test_deepfetch_host_failure_diagnostics_survive_worker_and_restart` 验证宿主诊断经 worker 持久化/重启。受控故障不等于实际下载供应商验收。
- **C1**：`tests/test_tool_contract_alignment.py::test_public_content_keeps_three_distinct_pagination_endings_and_exact_errors`。
- **C2**：同文件 `test_owner_rejection_details_and_original_effect_reconciliation_survive_error_wrapper`，真实资产 retirement 被 active references 拒绝，文本/结构化保留引用定位，原完整退休命令对账 not_found，原内容仍可读。fixture 使用受控 scope，不宣称运行中的模型处置。
- **C3**：同文件 `test_stage_note_pointer_is_callable_without_stripping_display_arguments`、`test_target_note_pointer_uses_its_real_authorized_owner_handler`，实际 reader/Owner，无需去除展示字段才能调用。
- **W1**：`tests/test_companion_writing_request.py::test_configured_model_is_callable_through_the_actual_assistant_grant`，以及 `tests/test_tool_failure_diagnostics.py::test_content_model_failure_explains_the_blocker_without_exposing_provider_data`；真实 Companion grant/注册工具/gateway，外部 HTTP 由受控端点或 opener 注入。缺配置修复继续测试与 27 passed 结果见交付报告；未调用真实内容模型。
- **D1**：`tests/test_tool_failure_diagnostics.py::test_source_tool_retains_external_diagnosis_and_corrects_same_job`，`test_external_schema_failure_is_not_an_argument_repair`、`test_external_unresolved_contract_preserves_cause_through_direct_call`。使用受控 ScopeAuthority，保留原 source/job 与失败 receipt，区分提交参数与损坏 schema；不把这类探针当作全部外部工具已通过。
- **L1**：`tests/test_owner_diagnostic_propagation.py::test_literature_owner_details_survive_and_original_channel_can_continue`，最终测试真实 Idea grant；baseline red 1 failed，candidate green 1 passed，见 `owner-idea-red-263` / `owner-idea-green-263`。
- **E1**：`tests/test_additional_tool_diagnostics.py::test_environment_reference_diagnostic_corrects_in_original_grant`，真实 Idea grant/Environment Owner；无引用与双引用均定位两个互斥字段，原通道改为准确引用读到原登记、原 effect 对账不变。
- **T1**：同文件 `test_recorder_offset_diagnostic_corrects_in_same_frozen_job`，真实 Recorder frozen job/grant，越界 offset 给出上限及修正页指针；同 job 回到合法页后继续，原 basis/source hash 不变。另 `tests/test_role_chain_diagnostics.py::test_recorder_owner_diagnostic_reaches_both_consumers_and_same_frozen_job_continues` 使用受控 Owner I/O 错误验证透传与原 frozen job 续接。
- **P1**：`tests/test_additional_tool_diagnostics.py::test_owner_pagination_details_survive_actual_reader_and_grant` 的 history/relations/human/stage 四个真实 reader，旧 revision 的具体原因透过各自包装；Stage 对外字段为 index_offset，Human 为 guidance_offset，纠正同时处理字节 offset；Stage 还验证缺 stamp 后使用原首页 revision 成功续页。
- **C4**：同文件 `test_context_page_reports_field_without_echoing_content_and_continues`，真实 Stage reader 的不存在 path、UTF-8 中间 offset、过小 limit，分别定位具体字段，错误不回显 selector/正文；修正后原内容不变。`test_creation_source_missing_selector_is_argument_error_and_original_basis_survives` 先在真实 protected creation grant 读到原 basis，缺 material_key 准确归参数错误，补准确 selector 后同 basis 内容/hash 不变，并验证共享 context offset 错误透过创建包装。
- **Q1**：同文件 `test_self_relation_rejected_then_original_effect_can_be_corrected`，真实 Reasoning grant 自关系拒绝先原 effect 对账 not_found，再选择失败前已存在的另一 Question，原 effect 接纳及对账一致；不为关系新造 Question。
- **F1**：`tests/test_discovery_failure_diagnostics.py` 的真实 Idea grant 覆盖 Dataset/Environment filter、Dataset read 引用互斥、Baseline 深引用缺 relation、五种 discovery revision 纠正，以及 Literature 将真实未取得全文维持为 missing 后继续。Dataset 存储故障为明确注入，恢复后保留原登记的 effect 对账；不把全局 Dataset identity 当成本 Quest 中已有 version。`263-discovery-contract-green2` 共 14 passed，含下面 M1。
- **M1**：`tests/test_role_chain_diagnostics.py::test_creation_grant_corrects_copy_limit_and_reconciles_without_overwriting_work`，真实 protected creation grant；copy 尺寸失败先原 effect 对账 failed，再以新合法命令复制，保留已编辑副本；已有 copy 的冲突不覆盖有效工作。
- **A1**：`role-chain-validation.json` 的实际 passed 节点：`test_acquisition_root_workflow::test_initial_connection_failure_has_formal_receipt_and_original_effect_reconcile`、`test_unknown_native_effect_is_reconciled_without_repeating_download`，以及 `test_public_acquisition_session::test_acquisition_request_reconcile_replays_lost_finish_ack`、`test_restart_reconciles_unknown_acquisition_without_replaying_provider`。这里的模型输出受控，不能据此声称真实供应商下载通过。
- **H1**：同一 Linux/JUnit 索引的 `test_public_semantic_mcp_gateway::test_typed_harness_admission_scopes_a_real_mcp_owner_query` 和 `test_lost_effect_response_reconciles_before_replay_without_duplication`，后者实际调用 host_compute.observe/reconcile 并验证只 probe 一次；文件 16 passed 不是全部 conformance 业务操作逐个故障通过。
- **U1**：同一索引的 `test_human_guidance_continuations::test_large_legacy_guidance_keeps_original_binding_and_requires_full_utf8_coverage`、`test_review_child_cannot_receive_guidance_read_or_feedback_authority`、`test_actual_bundle_target_batch_has_its_own_cut_on_the_same_rolling_job`，以及 `test_human_guidance_providers` 的实际 Idea/Plan/Reasoning/Target frozen cut 通过节点。该证据保留 operation/delivery 绑定及角色权限，不把方法目录存在等同于每个 effect 对账分支执行。
- **O1**：`tests/test_owner_family_diagnostics.py::test_bundle_implementation_conflict_reconciles_original_content_and_continues` 与 `test_plan_evidence_owner_failures_keep_exact_binding_and_same_grant_continues`，真实 Bundle/Plan grant 与 Owner。前者真实内容冲突和原命令对账，后续存储不可用为明确注入；后者三处 Owner I/O 注入后原 version/hash/role 与原文保持。`263-owner-family-green` 两个新增节点通过，连同既有相关节点共 12 passed；另一个旧 catalog 断言失败单独保留，不当作新诊断失败或整命令全绿。

E1/T1 最初 3 个 red 来自 `263-additional-diagnostics-red`，随后与相关既有回归共 44 passed。P1/C4/Q1 的有效旧版对照分别见 `263-reader-details-red2`（8 个有效诊断失败；另一个创建 fixture 的 capability 错误不计 red）及 `263-creation-selector-red2`（同 basis 先成功读取后，缺 selector 被旧版误报 unbound）。`263-reader-details-green` 的全部 12 个新增用例通过，连同既有 context/creation 共 **36 passed**；另 5 个 context_boundaries 在 setup 缺 `trajectory-before.json`，与 #291 `baseline-batch3/file-16.log` 相同，未执行业务断言，不计通过或新增回归。

上述已验证项只覆盖所写具体行为；未列出的独有业务分支和首次/修订差异仍按表中边界保留。真实模型消费仅以具体 N1 和 real-agent-consumption.json 为准，不从共同入口推定每个分支均已执行。所有测试记录的基线/候选版本及新增回归判断以最终验证报告为准。

- **S1**：`tests/test_submission_contract_diagnostics.py` 的 10 个真实授权节点覆盖 Target progress/raw 条件、材料 selection/absolute_path 互斥、Guidance delivery/effect 条件、HumanRequest 授权条件及公开分页别名、资产 intake/source selector。`263-effect-prior-state-green` 全部 10 passed，945 个源码/测试文件与 `beef1479` 全同。资产真实接纳后同 effect 畸形提交，只说明本次未开始、原效果 unknown；原完整 intake 对账仍返回相同接纳回执，正文不变。有效 red 为 `263-effect-prior-state-red`，不把此前未查账时的 not_recorded 误报当作正确结果。
