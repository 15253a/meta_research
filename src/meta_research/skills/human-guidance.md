# 正式人类指导

当前操作只接收系统按已认证 Quest 冻结的指导。调用 human_guidance.read 查看 delivery_ref、原件版本、力度、needs_treatment 和既有反馈，再用 delivery_ref 与稳定 effect_id 精确读取原件。每页沿 next_offset 继续，直到 full_read=true；摘要、索引、hash 或 prepared 不表示已读。新指导最多 64 KiB，可一次完整读取；较大历史原件须读全所有页。读取与反馈的重试用相同 effect_id 和相同参数；结果不确定时调用相应 .reconcile 查询原回执。

力度是人类表达的研究取舍。1「供参考」需认真读，可自由取舍并说明；2「有所倾向」优先考虑适用性，偏离时说明；3「优先考虑」默认采用适用建议，调整时交代证据与理由；4「强烈要求」在当前目标和授权内尽量满足，确有冲突或不可行时说明；5「按我设定」表达目标或方向应按原文对齐，保留具体差异与待对齐事项。力度不替代权限、完成证据或原 Owner 合同。

needs_treatment=true 时，完整读取后由根 Session 用 human_guidance.feedback 声明 understanding、changes、continuing_work、reasons，并选择 applied、considered 或 deferred；力度5可选择 goal_alignment_pending。写明实际理解、采用或调整、仍继续的工作和理由，保留未解决的冲突。反馈是根的声明，不是已落实的独立证明。读取、反馈或 goal_alignment_pending 都不表示目标已修改或切换；需要改变时，另行完成下述 Quest 目标演化。

## Quest 目标演化

调用 `research_graph.quest_goal.read` 读取当前整个 Quest 的目标、完整完成标准、持续条件、指导对齐状态，以及本次已认证逻辑操作冻结的实际工作切面。该切面在同一操作内不可更改；新 Target、生命周期或运行条件使它过期时，保留 stale 结果，由新签发的根操作获得新切面，不刷新旧调用。

用 `research_graph.quest_goal.evolve` 以一个判断同时提交替换目标和完整完成标准、对每条现有持续条件的保留或精确人类替代、对冻结切面每项工作的安排，以及后续方向。科学原因要么是本操作已完整读取的精确指导 delivery，要么是已接纳且在 RG 中有本 Quest 用途的精确证据版本。持续条件只能由更新、已完整读取的人类原文精确替代；后续的证据驱动演化仍继承它们。

工作安排覆盖 read 返回的每个可行动句柄：继续价值工作，在根阶段边界结束当前阶段，阻止未启动 Target，或停止精确 Target/run/generation。每个 stop 同时给出产物保留判断：`selected` 仅引用已经 RM 精确读取、managed 保管且 RG 有用途的真实产物；`pending` 明确后续要审查的工作区；`inspected_none` 说明已检查而无需保留。Target 工作区的规范说明可先用普通 Asset intake 的 `target_workspace_note` 选择器按原始字节接纳，再经 RG 作用关系选入；取消本身不伪造 Run、Evaluation 或完成产物。

effect 不确定时，用相同 `effect_id` 和字节等价判断调用 `research_graph.quest_goal.evolve.reconcile`；不改动 effect_id 重做。成功演化之后，系统以实际 AR 结果异步执行停止或阻止；Agent 不把意图当作已完成。

needs_treatment=false 时，该指导仍作为本工作的持续约束，读取原文与 prior_treatment 后继续履行，勿重复反馈。独立阶段工作与并行根各自处理，native Session 的沿用不合并责任。同一逻辑操作恢复沿原冻结快照继续；后来提交的指导由下一操作接续。工具只接收本次 delivery_ref 和 effect_id，Quest、根、job、operation 和快照由认证通道绑定，不能由工具参数选择。

HumanRequest 的回复仍归属于原请求根及原等待事项。普通聊天、历史研究说明、Agent 建议和 HumanRequest 回复不自行转成正式指导。独立 review 子智能体可以提供自由格式建议，由根亲自读取指导并声明处理；review-only 通道不授予指导效果权限。
