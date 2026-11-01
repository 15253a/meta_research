# Bundle 的 Owner 操作

读取、候选接纳、执行调度、恢复和收口均使用当前 scoped 工具目录与参数 schema；本文件描述职责，不发明可直接调用的伪函数名。合同和产物语义见[Bundle 契约](contract.md)，Target 的字段由实际注入合同提供。

## 职责

读取、加工及保管工作资料时，读[材料与保管](../../references/materials-and-custody.md)；涉及人类提交或指导时，读[人类协作指导](../../references/human-guidance.md)。原 HumanRequest 材料归原请求根；后继使用选中内容的精确 reader 与用途，不把未选副本带入 Target 常规产物。

AE 管阶段 request、epoch、BundleReport 与 StageCommit；RG 管 FormalPlan、Target／依赖／frontier、方法、Run／评价及领域接纳；RM 管不可变内容、AssetVersion、保管和完整性；AR 管执行、根会话、Fence、single-flight 和恢复；Harness 运行原生工具与子智能体。Bundle 管候选、范围、顺序和跨 Target 依赖，Target 根管实际实施与最终交接。

子智能体在明确任务及所授权限内读取、分析、实施或整理，按文件／对象划分写入，返回结果、精确来源和自由格式反馈。整理已接纳材料可沿已授资源接口登记 Dataset／Environment。普通 Literature 沿 research_memory.literature.register／reference 登记来源、原件及阅读用途，再用 .read 与 content.read 查回准确记录和内容。遵循共享研究导航的 Literature 合同；接口不可用或失败保留具体受阻步骤，不算无候选或已完成。未接纳 Target 中间产物仍留工作区，不提前进入 RM／RG 正式内容链。

确需改变 Quest 整体目标或完成标准时，读[正式目标演化流程](../../references/cycle-work.md)，判断实际变化并沿有效基准继承未变内容。本阶段仍核对本操作冻结的 Goal／条件／工作切面，不确定效果沿原操作对账。新工作使切面 stale 时，由新逻辑根操作取得新切面；不改写已接纳 FormalPlan 或旧调用。

## 正常流程

1. 观察当前 Bundle 请求及运行绑定，读取精确 FormalPlan、内容 hash 和直接绑定它的 current receipt。
2. 提出科学候选和真实依赖，由 RG 接纳身份和 spec；重读其内容绑定及 current frontier。
3. 仅对未启动、ready 且获授权的 Target 发起 admission／dispatch。claim 验证 spec、输入、资源、授权和 Harness，并确保至多一个 current 根。
4. 已执行工作沿原 TargetRun 观察、wake 或 reconcile；wake 只带重新读事实的信号，不携带研究结果作为权威依据。
5. Target 形成稳定交接后，由 AR／RM／RG 核验、保存并接纳真实 Run、产物和适用评价；Bundle 读回 TargetCommit 后更新覆盖。可复用资源的取舍及完成条件见[主 Skill](../SKILL.md)，登记和读回按[资源整理与正式登记](../../references/materials-and-custody.md#资源整理与正式登记)。
6. 独立审阅最终策略及交接，提交真实 BundleReport 候选，由 AE 验证阶段收口。

根与系统的执行分工不要求 Agent 重建 receipts、固定内部调用签名或 daemon 私有流程。仅凭 Session 完成、退出码、日志标签和页面进度不能认定正式科学结果。

## 同根后续输入

新根首次接收完整科学输入、角色指导和准确材料入口。每次业务 user 先给出本次绑定的 Cycle／Bundle 阶段／重心简短定位；原根收到 dispatch、target-batch 或其他实际变化时，其后追加本轮任务、变化及其来源：新接纳／修订的候选、当前可行 frontier 的相关变化、实际在途状态、新 TargetCommit 及研究说明入口、拒绝／阻塞／人的正式输入。沿同根已保留上下文连续安排工作，确需原文时读取本次准确绑定；不为每个批次重新拼接全份 Plan、全部 Target 历史和全部手册。适用指令、tools、权限、schema 与有效历史仍由运行时维持，业务增量不等于完整模型请求仅有增量。

本次安排所依据的 frontier、真实在途／阻塞／未知效果与尚未覆盖的 Brief／cell 标识须清楚呈现，新接纳结果携带准确 Commit 与正文入口；无需重复未变的完整合同与结果历史。已具备的依据按来源读回，会话记忆不代替当前事实；实际派发资格仍由 Owner 核验。

operation 的准确输入由宿主按共同冻结合同保存或引用，不能依靠这些增量文字或解析旧 prompt 还原。原 operation 的结果按其原绑定解释；当前能否接纳、派发仍由 Owner 按当前事实判断。checkpoint 和 frontier 是有来源的执行事实或继续工作的线索，不是可覆盖正式 Plan、TargetCommit 的第二份研究权威。

普通工具错误直接依据错误状态与信息修正调用；正式候选拒绝修订可修改内容。在途、recovering 或 finalizing 工作沿原 TargetRun 观察与恢复；未知效果先核对原身份，不当作未执行。正式求助答复和适用主动指导按共同人类协作入口交回相关原根，只有确实需要新增输入或恢复时才续接，不因独立审阅固定制造另一次 resume。压缩后按成熟 Harness 的会话与可达材料接续；缺少必要原件时如实处理。

新结果改变尚未提交工作的安排时，用现有 StrategyUpdate.notes 留下依据、保留项和未决内容；同一次最终 notes 保留仍有价值的前序线索。全部 Target 接纳后保留 Bundle 汇总回合，读最后结果、完成资源整理与独立审阅，再以空 candidates 和 strategy_complete=true 交接。无新候选不是科学耗尽，具体收口按 Bundle 契约判断。

## 恢复、取消和反馈

同一幂等身份仅对应同一 payload，已冻结交接仅查询原 identity，不从活动工作区重新 capture。取消需授权、投递与真实确认，迟到事件保留为历史观察，不形成新 current effect。Host pause／resume 沿权威控制事实进行，Agent 不读取或修改私有 SQLite、spool、密钥或控制文件来伪造恢复。

| 结果 | 动作 |
| --- | --- |
| `accepted` | 保存精确引用并重读 frontier／coverage。 |
| `rejected` | 读具体字段与原因，修订当前候选中可修改的内容；冻结输入按下述分支处理。 |
| `stale` | 重验 request、spec、输入、claim 和 handoff，不切到 latest。 |
| `needs_input` | 按精确 HumanRequest 的正式处理结果与等待解除条件接续；原根实际恢复后读 resolution，判断继续、调整或仍待协作，未满足条件不视为已满足。 |
| `outcome_unknown` | 对账原 identity，明确结果前不重放。 |
| `technical_blocker` | 保留具体范围和恢复条件，可修复则沿同一工作继续。 |
| `idempotency_conflict` | 保存冲突并停止该写入。 |
| `already_accepted` | 验证同 payload／hash 后复用原接纳事实。 |

反馈指向已接纳 FormalPlan／ContextPack 时，沿精确 reader 读回原件并核对绑定。若确认是冻结输入与系统校验不兼容、修订当前候选无法解决，保留原件、候选和审阅结果，携带错误码、字段、绑定及已核对事实提出 `system_operation_help`，说明所需系统修复和恢复条件；不截断或重写已接纳 Plan，也不把技术故障改称科学上的 `replan_required`。同一事项沿已打开请求的当前修订或正式后继接续，按 Owner 等待状态交接，技术重放不重复新建求助；原根实际恢复后沿保留会话及正式恢复身份核验修复结果，再决定继续或补充仍缺条件。普通候选错误仍自主修订，未受阻且已授权的独立工作可继续。

外部 MCP 的发现或调用失败按本回合外部 MCP 上下文中的[故障协作与恢复](../../external-mcp-recovery.md)处理。Bundle 判断自身工作和各 Target 的实际依赖，仅受影响 Target 由其原根求助与核验；安排其余 ready 工作。人的修复或替代选择经正式答复交回原根，新逻辑操作取得当前配置，旧操作沿原快照对账。

认证 Web 展示有界事件、当前／历史身份和连接状态；断线、重连、日志缺口或重复不改变研究事实。正式结果仅来自 Owner 验证的接纳链。
