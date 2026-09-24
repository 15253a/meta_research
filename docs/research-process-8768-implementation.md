# 8768 研究过程优化实现说明

本说明对应 `research-process-8768.md` 的七项需求。实现基于保留线上修复的 8767 源码快照，在独立 worktree 中完成；部署结果和完整测试套件的历史问题另行记录。

## 1. 研究目标、问题与 Cycle

共享提示和阶段技能明确：Quest 是项目目标，Question 可以宽泛、相互包含或关联，Cycle 是一段实际研究与复盘。问题跨多个 Cycle、暂放、分解和转向由 Reasoning 判断，不要求每轮解决问题。

包含关系继续使用已有 `parent_question_ref`。新增 `rg_question_relations` 和 Reasoning 专用的关联读取、记录、重放查询工具，以 `related` 加说明表达横向关联或重叠；校验双方为同 Quest 的精确已接纳 Question，保存来源 Reasoning Run、StageRequest 和内容 hash。同一 Run/effect 的重放返回原记录，内容变化会冲突。Reasoning 沿用实际 `NextCycleProposal.target_question_ref` 选择当前或其他可继续的问题，并确定下一 Cycle 的入口阶段；这些选择进入 Owner 持久化流程。

主要位置：`question_relations.py`、迁移 `0050_question_relations.py`、Reasoning 技能和 `root_capabilities.py`。

## 2. 实际工作、投入义务与阶段职责

Idea 提构想，Plan 选择本轮投入，Bundle 组织工作，Target 实施研究，Reasoning 综合判断。技能要求保存尝试、失败、调整、观察和未决事项，允许本轮没有新认识；obligations 表达调查投入和复盘责任，不保证问题解决。

Target 后端上下文只选取该候选实验对应的 Brief 和 obligations，准入校验局部工作及必要固定条件。整体 Plan 的覆盖检查位于 Bundle 宣告策略完成时，不作为每个 Target 的共同启动门槛。

## 3. 五类人类协作与回复接续

保留 `library_reconnect`、`external_material_api_access`、`offline_action`、`capability_authorization`、`system_operation_help`。其中 `offline_action` 明确可用于导师式研究判断和专家帮助。请求说明已做工作、结果、困惑与具体需要；人类意见与实际实验结果、正式授权分别记录。

新增所有阶段及 Target 可用的 `human_request.read`：根据已认证 Root 推导 Quest，默认分页查看最近请求和回复摘要，也可按精确 `request_ref` / `response_ref` 读取完整内容及 hash。UTF-8 字节分页保持原文，跨 Quest 或不匹配的回复引用会被拒绝；回复是否进入 Owner 评价也可读取。交接说明保存所采纳意见及其精确引用。

等待暂停的是发起请求的本地 Root 操作，其他独立 Target 可继续。Bundle 在等待前安排不依赖答复的工作；这不承诺已经暂停的同一 Root 仍继续执行工具。

## 4. 定义、实际执行与评价各有归属

Baseline、Variant、ProtocolVersion、Evaluation 保留为方法和评价定义；VariantRun 记录实际执行，EvaluationAttempt 记录针对指定 Run／产物的一次评价，MetricResult 只对应形成的评价结果。`formal_runs` 支持执行与评价交错、执行后暂不评价、复用精确已接纳 Run，以及真实执行或评价失败。

无评价时 `evaluation_attempt_ref` / `metric_result_ref` 为 `null`，失败评价也不伪造 MetricResult。数据库、Target 完成记录、TargetCommit、Bundle 消费者和查询链路均按实际可空关联处理，已修复只改展示却仍生成虚假引用的路径。提供 `formal_runs` 时，以各 `evaluations[].metrics` 为实际评价来源，顶层 `metrics` 可为 `{}`，无需重复填写。

Run 和评价分别声明 `artifact_paths`，checkpoint 绑定精确执行状态。仅在实际生产者唯一时采用惯例路径默认归属；多生产者须明确分配，未归属的 Target 说明仍保留为 RM 交接内容。复用旧 Run／评价不会把当前 Target 的新文件归到旧执行名下。

主要位置：`formal_entities.py`、`bundle_protocol.py`、Research Graph、Target 完成链路和 Target 的 `formal-work.md`。

## 5. RM／RG 的责任与关键约束

RM 管理文件、内容和版本，RG 管理研究身份、关联、来源与接纳事实。新 Question 关联采用单表与既有来源证明；不为探索判断新增多层审批或证明。实际执行和评价复用既有 Owner 接纳边界。

保留精确版本与内容一致、来源可追溯、真实执行／评价状态、关键外键、提交与重试幂等约束。动态研究内容的读取接口不强制复制一套庞大输出结构；固定输入和正式合同仍按其边界校验。

## 6. 统一、轻量的交接入口

各阶段默认展示继续原因、尝试与失败、未决范围、精确资产引用及人类意见读取入口。前驱 Reasoning 交接可进入任意后继阶段；Idea/v4 合同接收轻量交接说明和研究 notes，完整来源仍由 Owner 验证。

阶段默认摘要预算为 64 KiB；完整必需输入单独保留，不受摘要裁切影响，因此完整视图可能超过这一预算。前驱 notes 和 scientific_summary 的默认摘要分别截取至 2048、4096 字节，并保留 `predecessor_closure` 精确读取入口。摘要不修改原始历史，长文本按需展开；已验证真实 Reasoning 结论中的人类回复引用进入下一 Cycle 的 Idea 上下文。

主要位置：`context_presentation.py`、`idea_contract.py`、`human_research_context.py`。

## 7. 长期读取成本与可判断状态

Target 统一使用 `agent_runtime.target_run.progress`：首次返回有界日志尾部，后续复用不透明 cursor 读取新增状态、片段和异常。Agent 根据实验阶段、预期输出、异常迹象、延误代价和读取开销自主决定下次观察时间；接口返回的间隔只是可选建议。Agent 可以中途检查、评价已有数据或 checkpoint，并自行决定是否继续、局部修正或提前停止当前实验，遵循既有研究范围与授权。原始训练／评价日志保留，重试使用独立文件；`raw` 模式按精确 `log_ref`、`stream_ref` 和字节偏移读取历史。日志观察不等于研究结果已被 Owner 接纳。

每次最多返回 8 段、16 KiB 日志正文，cursor 最多追踪 64 个流。持续增长的前部日志不会饿死后部日志；截断发现范围轮转时会有界淘汰旧追踪项并提供通知，返回的 cursor 可继续读取，不删除原始文件。

界面分别展示输入、实际执行、产物、评价、接纳、交接、求助七项事实，区分研究负面／不确定结果、技术故障、等待人类与已接纳记录；执行已记录而评价待办可直接识别。展示只采用匹配当前 Target、Run 和 spec 的 Commit。日志及长期 Session 默认保留最近 512 KiB 可见窗口并支持读取历史；Target 图、结果与历史上下文使用有界展示和精确入口，避免默认反复展开完整记录。

主要位置：`target_progress.py`、`target_run_semantic.py`、`targetResearchFacts.ts`、`RootConversations.tsx`。

## 已取得的定向验证

- `test_research_process_8768.py`：11 例通过，覆盖五类 Root 入口的精确人类回复读取、跨 Quest 拒绝、Question 关联稳定重放及来源一致性、真实 Owner 跨 Cycle 交接、超长前驱视图预算。
- 工具目录、阶段交接与上下文边界定向组：35 例通过、6 例排除。其中 5 例依赖缺失的历史快照，1 例为随后单独调整的动态输出 schema 断言；这里不将排除项计作通过。
- 上述交接与测试增量的 `git diff --check` 通过。定向结果不代表完整套件全部通过，也不代替部署验证。

## 两轴审查与处理结果

| 审查轴 | 初始发现 | 已完成处理 | 当前未解决 |
| --- | --- | --- | --- |
| Standards | 1 项 P2：发现范围轮转后 cursor 可能累计超过 64 个流，下一次无法读取 | cursor 追踪项保持有界，淘汰附通知，保留原始日志和后续读取能力 | 0 |
| Spec | 1 项 P1：无评价或失败评价仍存在虚假正式引用；2 项 P2：产物归属不准确、cursor 轮转失效 | 真实 Owner 写入与消费链路采用实际可空关联；产物绑定真实生产者，复用不改归属；修复 cursor 有界轮转 | 0 |

以上状态指两轴审查提出的具体问题已修复，不扩大为对未运行检查的通过声明。
