# 正式人类指导

## 工作资料的当前处理与后继交接

HumanRequest 答复始终由原请求根处理；软指导附带材料、主动意见与新闻论文先交给提交时绑定的当前研究根，不需要伪装成求助答复。Agent 自行取得的服务器材料用 `research_workspace.materials.acquire` 提交 `absolute_path` 由系统安全检查生成引用，或提交既有原样 ServerSelection，并说明真实取得路线；材料绑定本根，不按后来前台改投。三种来源均先作为工作资料；保存引用、收到、实际读过、采用与足以支持科研结论是不同事实。新闻、人的判断、论文及实际实验产物保留各自性质。

先用 `research_workspace.materials.discover` 发现可见 reference_ref，按需要展开目录，再以 observation_ref 调 `research_workspace.materials.read`，沿字节范围继续。未读、只读局部、缺失或权限失败均如实说明。需要修改或轻量试跑时，用共同 `research_workspace.materials.copy` 按需取得单个独立可写副本；返回的 working_path 可用本根原生工具处理，重试用同一 effect_id 或 `.copy.reconcile`，不会覆盖已编辑副本。运行根副本在 `.work-materials/`，临时结果也可放此处；不要把未选材料移入 Target 的常规 outputs 目录。原件保持完整，不递归复制或预读整个目录。

由接收根先完成当前可做的初读、判断和处理，用 `research_workspace.materials.feedback` 声明 understanding、disposition（adopted／considered／deferred／not_used）、changes、continuing_work、reasons 和 limitations。采用必须有该根实际读取记录；反馈仍是根的声明，不是科学支持、目标完成或独立核验证明。说明对当前安排或判断的实际影响、依据与未采用理由，保留未读范围和未决事项。可分次补充，以不同稳定 effect_id 表达真实后续处理；未知结果先 `.feedback.reconcile`。

selections 按研究价值选择原件、处理结果、两者或 `[]` 均不保管。每项写实际 purpose 和 managed／linked_local custody：原件 source 为 `{kind:"original_file",path,observation_ref}`；处理结果从工作区发现页取得 `{kind:"workspace_file",workspace_ref,path,expected_sha256}`。选中项沿既有 RM 内容接纳及 RG source-material 用途关系保存，返回 asset_binding、role_ref 和精确 reader。linked_local 留在实际位置，后续来源变化如实报告漂移；未选副本与输出不会因反馈或 Target 收尾自动入库。真实 Target 实施、评价和产物仍沿正式结果交接，不由材料反馈虚构 Run、MetricResult、Evidence、Dataset 或 Environment。

后继根在本 Quest 的 `materials.discover.retained_treatments` 检索真正选中的内容、原始 receiver、实际处理根、用途与影响，沿 next_offset 分页，再把 selection.reader 交给 `research_memory.content.read` 精确读回；原投递不重新绑定。材料较多时可委派聚焦工作，根负责当前初读、影响判断及必要交接。需重做上游或下一轮投入时，由现有交接经 Reasoning 安排后继 Cycle；不因收到材料强制新建 Cycle，也不在同轮回退阶段。

正式指导附带材料及其 retained_treatments 保留 source_guidance 的原文、助手理解、确认范围、力度和保留条件。交接后继续保留该边界；材料可供后继读取研究背景，不能把只适用原 Cycle 或 Target 的要求扩大到后继工作。材料本身可按研究价值独立采用和验证，既有科学接纳合同不变；scope_confirmation=legacy_unconfirmed 的历史来源如实保留未记录范围确认的事实。

## 正式指导的冻结读取

调用 human_guidance.read 查看本次冻结指导的 delivery_ref、原件版本、力度、semantic_scope、applies_to_work、needs_treatment 和既有反馈，再用 delivery_ref 与稳定 effect_id 精确读取人的原文、助手理解及保留条件。每页沿 next_offset 继续，直到 full_read=true；摘要、索引、hash 或 prepared 不表示已读。新指导最多 64 KiB，可一次完整读取；较大历史原件须读全所有页。读取与反馈的重试用相同 effect_id 和相同参数；结果不确定时调用相应 .reconcile 查询原回执。

归档 Quest、助手阅读上下文、材料 receiver 与确认的 semantic_scope 各司其职。只有 applies_to_work=true 的指导成为本工作要求：Quest 范围适用整个 Quest，Question 范围接续该问题的后继 Cycle，Cycle 范围只接续该轮阶段，Target 范围只适用该 Target。background_only=true 的其他指导可读背景，保持自己的工作要求。没有适用活动工作时，指导保留供正确后继消费；阶段切换、并行和恢复沿冻结范围接续。scope_confirmation=legacy_unconfirmed 表示旧记录没有保存范围确认；按旧 Quest 指导及原文读回，不能声称人当时确认了新范围。

力度规定已确认范围内的遵循程度。1「供参考」需认真读，可自由取舍并说明；2「有所倾向」优先考虑适用性，偏离时说明；3「优先考虑」默认采用适用建议，调整时交代证据与理由；4「强烈要求」在授权内尽量满足，确有冲突或不可行时说明；5「按我设定」严格遵循确认范围内的原文要求，保留具体差异与未解决事项。力度不决定作用范围或 Quest 目标是否改变，也不替代权限、完成证据或原 Owner 合同。

适用且 needs_treatment=true 时，完整读取后由根 Session 用 human_guidance.feedback 声明 understanding、changes、continuing_work、reasons、disposition 和实际 goal_impact。goal_impact=none 表示整体目标及完成标准保持；requires_evolution 表示确需整体演化，可选 goal_alignment_pending；undetermined 保留待判断事项。各档力度均可影响目标判断。写明实际安排与判断依据，保留未解决冲突；反馈是根的声明，不是已落实的独立证明。局部严格指导无需空目标版本，也不因此阻止整体完成。超出局部确认范围的整体要求应先重新供人确认；研究 Session 可以在范围内细化安排。已确认 Quest 范围指导尚未判断或确需演化时仍待目标对齐，读取、反馈或 pending 本身不表示目标已切换。

## Quest 目标演化

调用 `research_graph.quest_goal.read` 读取当前整个 Quest 的目标、完整完成标准、持续条件、指导对齐状态，以及本次已认证逻辑操作冻结的实际工作切面。该切面在同一操作内不可更改；新 Target、生命周期或运行条件使它过期时，保留 stale 结果，由新签发的根操作获得新切面，不刷新旧调用。

用 `research_graph.quest_goal.evolve` 以一个判断同时提交替换目标和完整完成标准、对每条现有持续条件的保留或精确人类替代、对冻结切面每项工作的安排，以及后续方向。科学原因要么是本操作已完整读取、确认范围覆盖 Quest 的精确指导 delivery，要么是已接纳且在 RG 中有本 Quest 用途的精确证据版本。局部指导不能充作整体变更授权。研究 Agent 仍可结合研究风格，基于接纳证据自主演化；此证据原因保持独立，无需新增助手或人的一律确认。持续条件只能由更新、已完整读取且适用整个 Quest 的人类原文精确替代；后续的证据驱动演化仍继承它们。

工作安排覆盖 read 返回的每个可行动句柄：继续价值工作，在根阶段边界结束当前阶段，阻止未启动 Target，或停止精确 Target/run/generation。每个 stop 同时给出产物保留判断：`selected` 仅引用已经 RM 精确读取、managed 保管且 RG 有用途的真实产物；`pending` 明确后续要审查的工作区；`inspected_none` 说明已检查而无需保留。Target 工作区的规范说明可先用普通 Asset intake 的 `target_workspace_note` 选择器按原始字节接纳，再经 RG 作用关系选入；取消本身不伪造 Run、Evaluation 或完成产物。

effect 不确定时，用相同 `effect_id` 和字节等价判断调用 `research_graph.quest_goal.evolve.reconcile`；不改动 effect_id 重做。成功演化之后，系统以实际 AR 结果异步执行停止或阻止；Agent 不把意图当作已完成。

applies_to_work=true 且 needs_treatment=false 时，读取原文与 prior_treatment 后继续履行该范围内约束，勿重复反馈。独立阶段工作与并行根各自处理，native Session 的沿用不合并责任。同一逻辑操作恢复沿原冻结快照继续；后来提交的指导由下一操作接续。工具只接收本次 delivery_ref 和 effect_id，Quest、根、job、operation 和快照由认证通道绑定，不能由工具参数选择。

HumanRequest 的回复仍归属于原请求根及原等待事项。普通聊天、历史研究说明、Agent 建议和 HumanRequest 回复不自行转成正式指导。独立 review 子智能体可以提供自由格式建议，由根亲自读取指导并声明处理；review-only 通道不授予指导效果权限。
