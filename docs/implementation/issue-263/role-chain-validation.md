# #263 角色调用链与继续工作证据

本表核对错误从实际调用入口到原消费者的传递，以及保持原授权、版本和有效成果的继续路径。接口测试、受控 provider 输出与真实模型调用分别标注；不把任何一项等同于完整科研 Cycle。

**#263 本票源码验收完成。** 运行源码为 `beef1479d46b8639c747a9edbb54e78d9412d2b9`，真实模型和完整回归已执行。最终交付仅追加一项旧长句断言修正，373 个运行源码文件不变。[逐节点记录](role-chain-validation.json) 保留最终全套的实际结果和专项修正记录，包含失败；精确版本及归因见 [validation.json](validation.json)，不将旧失败或受控模型证据改写为成功。

## 实际角色与独有路径

所有研究根复用 `SemanticMcpGateway` 的 text/structuredContent 包装，差异在 live grant、组参、Owner、宿主恢复和结果消费。共享机制验证按机制复用；普通 Companion、创建材料 Companion 与 Recorder 的授权不能合并。

| 入口 | 组参、Owner、包装与消费链 | 已取得的有区分力证据 | 证据边界 |
|---|---|---|---|
| Idea | `idea_skill`／`idea_stage` 的 run/attempt/root/fence → 公开 reader → RG/RM → 原生 Harness MCP 消费 | 最终 `beef1479` 真实 `gpt-6.1-sol` 原会话纠正过期 content 版本，并在 literature 已接纳却坏回执后先原 effect 对账，再继续读取；`test_owner_diagnostic_propagation` 保留 Owner details | 真模型决定后续工具调用；初态由 fixture 建立，未冒充完整阶段／部署验收 |
| Plan | `plan_skill` 继承 rejection、原 submission/receipt 和 native session；公开 evidence page/read → RG 精确绑定 → RM 物化正文 → 原 Plan grant | `test_plan_skill_adapter::test_owner_feedback_is_present_in_both_successor_prompts` 保留原 session/反馈/回执；`test_owner_family_diagnostics::test_plan_evidence_owner_failures_keep_exact_binding_and_same_grant_continues` 验证 page/catalog/materialize 三个 Owner 故障原码/details，恢复后同 grant、同版本/hash/role 读取实际正文 | 前者受控模型 wire；后者真实接口和内容，I/O 故障注入。未声称 Plan 真模型独立完成全部研究流程 |
| Bundle | `bundle_stage` 持久拒绝 → `bundle_skill` 后继调用；exhaustion → AE；implementation accept/read/reconcile → RM | `test_bundle_correction_feedback` 拒绝旧 attempt/fence，当前调用携原 rejected operation/unit；`test_tool_bridge_diagnostics` 原提案冲突后对账已接纳回执；新增 `test_owner_family_diagnostics::test_bundle_implementation_conflict_reconciles_original_content_and_continues` 保留真实 conflict、差异字段、原 payload/revision/receipt，并验证读／对账 I/O 恢复 | 真实接纳和冲突，模型输出及 I/O 故障受控；`test_bundle_resident_mcp` 既有错误码断言失败仍保留 |
| Target | Harness/workspace → `TargetRunFinalizer` → RM 冻结、RG 正式输入/结果接纳 → AR rejection → 同根 completion generation；运行进度与材料操作沿当前 Target grant | `test_target_formal_input_revision_feedback::test_invalid_formal_input_is_rejected_and_revised_in_same_root` 四分支给 input_refs 原因，同 handle 修正完成且无部分 RG 写入；`test_target_root_finalizer` 保留真实拒绝及 replay；`263-effect-prior-state-green` 整文件 10 passed，覆盖 progress/raw、材料来源、HumanRequest、reader alias、资产入库与历史效果 unknown | Finalizer 证据读取器受控；既有 completion_publish 断言失败保留。busy_logs 19/20 collector 缺陷在基线和候选均复现，未修复，见下节 |
| Reasoning | `reasoning_skill`／worker 的短 code 与完整 feedback → 持久化 → 重启原 native session resume → 正式接纳 | `test_reasoning_diagnostic_delivery::test_production_wire_receives_durable_parent_feedback_then_delivers_after_restart`：超过 128 字符的准确 parent-question 反馈到第二次原会话调用并正式接纳；diagnostic_transport/rejection_diagnostics 另有通过节点 | 真实 worker/Owner，模型 wire 受控；#292 已实现的窄修复仍复核，既有 resident 授权夹具失败保留 |
| DeepFetch | `deepfetch_source_action` 冻结 source/job → acquisition 宿主调用 → `RootResidentMcpChannels` → AR 事件／REST → 原 worker | `test_tool_failure_diagnostics::test_source_tool_retains_external_diagnosis_and_corrects_same_job`；`test_tool_bridge_diagnostics` 的 Resident 分类及持久失败重启测试保留原 run/attempt/session，provider 仅执行一次 | 来源／故障受控；现场无外部来源配置，不声称真实供应商下载成功 |
| Acquisition | `acquisition_root` 的 session_mode/target_dir/冻结批次 → 原生获取输出与正式 HumanRequest／获取 Owner → 原 job/effect/native resume | `test_acquisition_root_workflow` 的初次连接失败正式回执、原效果对账和 unknown 不重复下载；`test_public_acquisition_session` 的丢 finish ack、重启 unknown、原 hold、无效产物终止；scope_claim 拒绝不消耗下一 attempt | 模型输出受控；旧 Node `--use-env-proxy` 和缺 CLI setup 失败仍保留 |
| Companion／Writing | 普通 Companion 只读研究材料；Writing 共享 Skill → 当前助手 grant → `assistant.content.generate` → `writing_content` → 原助手继续使用稿件 | `test_companion_literature_integration`／authorization_revocation 验证精确 reader 和即时撤权；native_rotation 保留会话工作；`test_companion_writing_request` 及工具诊断回归验证配置修正和 HTTP 分类；真实原生文件／patch／子 Agent 恢复见下节 | Writing 无独立 Owner 或新增权限；HTTP 受控，不冒充供应商实际故障 |
| 创建材料 Companion | `ProtectedCreation.channel` 专属 operation/fence → discover/read/copy/reconcile → HC observation／效果／见证 | `test_role_chain_diagnostics::test_creation_grant_corrects_copy_limit_and_reconciles_without_overwriting_work`：实际 Linux chroot、正式 grant；无效下界改正后原 effect 成功；payload 冲突保留原回执；大小不足先对账确认 failed，再按合法限额复制，既有编辑与原件不变；seal 撤权 | 实际接口及原生执行，非模型决定；旧目录断言失败保留，未删除 discovery 工具迁就测试 |
| Recorder | 原生摘要会话 → `RecorderChannels.grant` 的 job/basis/node/source/hash → `timeline.sources.read` → 专属包装 → 摘要消费者 | `test_role_chain_diagnostics::test_recorder_owner_diagnostic_reaches_both_consumers_and_same_frozen_job_continues`：Owner 暂不可读保留 source/hash 和短方向，恢复后同 job/basis 读完真实正文且 Owner revision 不变；新增 offset 测试纠正同一 frozen job | 故障明确注入；不把不可读改为空阶段，不允许 Recorder 修改研究事实 |
| Harness conformance | 精确 conformance scope → 5 个 Owner snapshot、host-compute effect/reconcile、quest receipt verify | `test_public_semantic_mcp_gateway` 的真实 admission scope、丢效果回执后对账、channel 重发／撤销和跨 root 拒绝 | 这些工具真实注册但不是普通研究根目录；旧 full_conformance 的模型期望等失败保留 |

## 跨角色的独有 reader 与提交条件

| 边界 | 调用者得到的具体修正与保留条件 | 测试入口 |
|---|---|---|
| Discovery | Dataset/Environment 的互斥筛选与精确引用；Baseline 深引用须指定 relation；Literature obtained 必须有实际全文 binding，不能把 missing 改造成 obtained；过期 query revision 从第一页重读 | `test_discovery_failure_diagnostics.py` |
| 上下文分页 | Question history/relations、创建依据、Stage context 保留 Owner code/details；`index_offset` 与字节 `offset` 分开，修正方向采用公开参数名；source view 缺少 `material_key` 是参数拒绝 | `test_additional_tool_diagnostics.py` |
| HumanRequest reader | responses 的 request_ref、constraint_ref/response_ref/cursor 互斥；guidance_offset、response_offset、input_offset/input_query_revision 与字节 offset 准确映射 | `test_submission_contract_diagnostics.py` |
| 有效果提交 | Target progress/raw 条件、材料 selection/absolute_path 二选一、HumanRequest required_authorization 条件及唯一 acceptance_conditions、资产 text/source/target_workspace_note 选择与来源字段 | `test_submission_contract_diagnostics.py` |
| 历史效果判断 | 11 处前置拒绝返回本次 not_started、历史 effect unknown；真实已接纳后同 effect 畸形提交仍先原参数对账并保留原资产。材料获取已查原 effect，才可报告 absent/not_recorded | `263-effect-prior-state-red` 真实失败→`263-effect-prior-state-green` 整文件 10 passed，945 个文件与最终源码一致 |

以上列出已验证的实际分支；逐节点结果及旧失败对照以最终 `validation.json` 为准，未将工具清单或文件存在本身作为通过依据。

## 两类真实原会话证据

语义工具 probe 使用 `beef1479` 的 `CodexHarnessAdapter`、实际 Idea grant 和生产 Owner。原会话 `01a1bc62-b09a-7932-8c27-8b8ad0fd8d36` 自行纠正 content 引用，并对真实 Owner 已接纳后的 literature 坏回执按原 effect 对账，再读目录和正文。7 次 CLI 工具结果（含 2 次失败）与 HTTP 参数及 text/structuredContent 一致，写入和 Owner 执行各一次。完整错误文本仅 285、563 字符。当次 945 个源码／测试文件与 `beef1479` SHA256 逐项一致，差异 0；之后只有一项测试断言改动，最终 373 个运行源码文件仍相同。临时凭据剩余 0，专用隧道已停止。见 [消费记录](real-agent-consumption.json)、[源码记录](real-agent-source-files.json) 和 [清理记录](real-agent-credential-cleanup.json)。旧 `da890f4` 会话另存 [历史证据](historical-da890f4/real-agent-consumption.json)，未冒充最终复验。

原生工具 probe 使用实际 Companion argv、CLI 0.159.0 和 `gpt-6.1-sol`：会话 `01a1bc3d-e23c-73c0-812f-cf47873644e9` 在文件不存在、patch 上下文错误、子 Agent 名不合法后分别纠正，并于第二回合继续同一子 Agent；已有成果 hash 不变，未伪造缺失文件。见 [原生恢复结果](native-recovery-summary.json)。这项 probe 的空 MCP fixture 不授予任何业务权限；[源码复用记录](native-source-reuse.json) 仅证明装配路径未变。

13 个实际 Harness 装配变体覆盖 8 个研究根及 Writing、Target workspace-write、DeepFetch/Acquisition provided_only、Recorder 差异。实际原生身份并集为 26 项（普通 23、provided_only 22、Recorder 3），与 96 个内置语义操作分别计数，详见 [逐工具表](tool-coverage.md)。

## 结果版本与范围

`263-owner-family-red` 两个新增节点在旧 `da890f4` 均因 Owner 错误误归 host_failure 失败；`263-owner-family-green` 新两例通过，合跑 12 passed、1 既有 catalog 失败，后者与 #291 同节点同诊断。创建材料／Recorder 初始补证为 2 red→2 green；`263-discovery-contract-green2` 包含后续复制大小分支，共 14 passed。reader 新增节点已通过；submission 最终历史 effect 修复整文件 10 passed。早期夹具字段接线错误保留且不算产品红证据，测试集重叠不累加。

最终完整运行 `263-final-reviewed-full` 固定 `beef1479`，归档 `854bf482080ed54563ae8470e29615ddfa8861bb5da691f48a8eeaa03a390454`：537 文件全部完成，4840 collected＝4356 passed／482 failed／2 skipped，0 incomplete，1149.47 秒。最后补齐的 5 个诊断测试文件共 38 个节点全部通过。482 项原始失败完整保留：480 项基线已失败；1 项旧长句断言仅修改测试以核对短诊断的精确 reader 修正信息，保留错误码、双通道一致、同 grant 续读和撤权检查，`263-concise-contract-assertion` 整文件 5 passed，Spec 确认未削弱；另 1 项 busy_logs 的 19/20 缺陷未修复。

busy_logs 直调 collector，不经过本票修改的 semantic 包装，6 个相关文件从基线到候选字节相同。`263-progress-order-baseline`／`263-progress-order-candidate` 用真实文件和受控 mtime 排序在两版均复现 8→16→19，属于新观察到的既有缺陷，未计为通过。最后一项 test-only 修正后没有重跑全套，不覆盖或重算原始 482 个失败。Standards 正式终审 0 项；Spec 源码／模型／清单和测试修正无未解决发现，最终审查记录见 [review.md](review.md)。

#263 的工具清单、独有角色链、原 Agent 修正／先对账继续与回归核对已经齐备，源码验收完成；目标分支为 `v1-test`，实际发布／关闭状态以远端为准。没有改变 8770 服务或研究数据，不等同于生产部署验收。#269 按目的分层发现尚未实现；#274 负责联合版本完整科研 Cycle、阶段接纳／交接及界面消费，不代替本票错误反馈证据。未配置供应商的实际故障、每个 Owner 全部业务分支、每个角色逐工具模型组合没有被宣称验证，也不是新增笛卡尔组合要求。
