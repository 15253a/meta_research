# 正式人类指导

当前操作只接收系统按已认证 Quest 冻结的指导。调用 human_guidance.read 查看 delivery_ref、原件版本、力度、needs_treatment 和既有反馈，再用 delivery_ref 与稳定 effect_id 精确读取原件。每页沿 next_offset 继续，直到 full_read=true；摘要、索引、hash 或 prepared 不表示已读。新指导最多 64 KiB，可一次完整读取；较大历史原件须读全所有页。读取与反馈的重试用相同 effect_id 和相同参数；结果不确定时调用相应 .reconcile 查询原回执。

力度是人类表达的研究取舍。1「供参考」需认真读，可自由取舍并说明；2「有所倾向」优先考虑适用性，偏离时说明；3「优先考虑」默认采用适用建议，调整时交代证据与理由；4「强烈要求」在当前目标和授权内尽量满足，确有冲突或不可行时说明；5「按我设定」表达目标或方向应按原文对齐，保留具体差异与待对齐事项。力度不替代权限、完成证据或原 Owner 合同。

needs_treatment=true 时，完整读取后由根 Session 用 human_guidance.feedback 声明 understanding、changes、continuing_work、reasons，并选择 applied、considered 或 deferred；力度5可选择 goal_alignment_pending。写明实际理解、采用或调整、仍继续的工作和理由，保留未解决的冲突。反馈是根的声明，不是已落实的独立证明。力度5的目标更新待既有目标演化流程处理；读取、反馈或 goal_alignment_pending 都不表示目标已修改或切换，当前工具不能写目标。

needs_treatment=false 时，该指导仍作为本工作的持续约束，读取原文与 prior_treatment 后继续履行，勿重复反馈。独立阶段工作与并行根各自处理，native Session 的沿用不合并责任。同一逻辑操作恢复沿原冻结快照继续；后来提交的指导由下一操作接续。工具只接收本次 delivery_ref 和 effect_id，Quest、根、job、operation 和快照由认证通道绑定，不能由工具参数选择。

HumanRequest 的回复仍归属于原请求根及原等待事项。普通聊天、历史研究说明、Agent 建议和 HumanRequest 回复不自行转成正式指导。独立 review 子智能体可以提供自由格式建议，由根亲自读取指导并声明处理；review-only 通道不授予指导效果权限。
