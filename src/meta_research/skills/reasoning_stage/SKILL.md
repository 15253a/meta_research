---
name: reasoning-stage
description: 综合本轮工作与研究历史，交接当前认识和证据，判断继续、改向或等待的理由，并给出正式后继入口。
---

# Reasoning：综合与选择后继

在本轮检查点判断当前 Question：现在如何理解，依据是什么，下一步为什么继续、改变或等待。可以没有新发现或保持原认识，说明其原因和投入含义；Cycle 收口不表示 Question 已回答。

输出和来源见[语义契约](references/contract.md)；权限与接纳见[Owner 操作](references/owner-operations.md)。遵循根系统提示的五入口、预算、人类输入和语言偏好，委派时传递范围及直接输出语言。输入已有正文无需为固定顺序重复读取。

## 1. 综合真实来源

先读本轮结果、上轮及相关问题历史，再按需分页、展开精确原文。保持 request、Question、Quest、Goal、epoch 和来源绑定。Idea／Plan／Bundle 的 Completed、Skipped、Exhausted、NoViableCandidate 是路线状态，本身不证明科学主张。

从冻结闭包、Plan 已验证来源绑定或同 Quest 可核验历史引用精确 kind／ref。LiteratureRecord、MetricResult、WorkProduct、日志、分析、checkpoint、人类输入、ScientificOutcome、AssetVersion 等按真实内容支持、反对或限定判断；材料类型和数量不决定科学采用资格。仍须核对 Owner、版本、Quest 权限和接纳链，不能用相近对象 ID 代替真实来源。纯理论综合可无外部引文，明确推导、适用范围和不确定性。

按事实选 `affirmed | denied | uncertain | insufficient_evidence`。affirmed／denied 限定 claim；uncertain 说明有效证据为何未收敛；insufficient_evidence 保持 `claim=null` 并说明缺失。部分证据与仍缺其他证据可以同时存在。未测量、科学证据不足、访问阻塞和程序故障分别表述。

用 `support_scope`、`limitations`、`causal_interpretation`、`research_synthesis` 和 notes 组织解释，短结论结合精确来源，不重复整个历史。区分事实未查清与风险受控、新认识与重复整理；反复无进展时重审缺口、策略或资源，而不是无理由生成新 Cycle。

聚焦阅读、历史召回和比较可委派独立子智能体，根抽查关键原文后综合。人类指导按真实内容影响判断，普通意见不自动成为授权或完成确认。完成条件：关键判断能追溯到实际来源，已有认识、局限和缺口明确，技术阻塞没有被写成科学结论。

## 2. 交接前沉积已接纳 Target 数据

读取已接纳 Target 的 `dataset_candidates` 和精确 completion manifest，沿 `artifact_path` 找到已冻结条目的真实 RM binding，判断独立复用价值。决定保留的候选在本次 Reasoning 交接前，通过当前 catalog 授予的 datasets.register／register_version／reference 登记 Dataset、精确版本和当前 Question 的实际用途；真实派生关系用 derive。Target 已正式接纳即满足这些原件的发布前提，primary／draft 阶段可做，不等本次 ScientificOutcome 接纳。

最后一个 Target 后 Bundle 若不再运行，由 Reasoning 承接。先发现或对账既有登记，再复用身份和原件；未知效果沿同一 `effect_id` reconcile。可委派明确资产范围的子智能体登记，根在交接前独立查回精确版本、用途引用、派生关系及原件。无保留价值时说明判断；外部条件阻塞时记录未完成步骤和继续条件；暂存、未接纳材料仍在工作区。

完成条件：值得保留且当前可登记的候选已登记并读回；其余有真实取舍或具体阻塞说明，不把空 Dataset 身份当作已有版本，也不把登记当作新实验结果。

## 3. 判断后继与相关问题

先决定研究需要，再选 `idea | plan | reasoning`。需重新构思进入 Idea；构想仍适用且要实施时，以可验证 Idea skip basis 进入 Plan，由 Plan 重新选择义务、复用和剩余工作；已有材料只需综合时，在前序 skip 合同成立后进入 Reasoning。下一 Cycle 不直接进入 Bundle，旧 Plan 的 coverage 和 Brief 不替代当前计划判断。

说明完成了什么、剩余问题及后继理由，并引用相关结果或入口。Reasoning 的复用建议不等于正式输入绑定；Plan 选择义务和证据，Bundle 为具体 Target 绑定所需资产。同一 Question 可以继续研究，也可暂放、复盘已有问题或转向相关题，均说明依据。

用 `research_graph.question_relations.read` 读取相关问题。有值得保留的横向关联或重叠时，经 `research_graph.question_relations.record` 提交同 Quest 两个精确已接纳 `question_ref` 与说明；包含关系仍用原 `parent_question_ref`。同一 `effect_id` 对应同一内容，结果不明先 reconcile；关系不表示问题已解决。

拟创建／分解值得近期研究的新问题且需要检索支持时，才走现有 AutonomousCreation 的 DeepFetch 路径。取得标准 `summary.md` 后，本 Reasoning Session 继续读精确摘要、调整科学判断及 Question 六字段，再决定 create／decline；普通综合和补充阅读不强制该路径。新题可宽泛、具体、包含或关联已有问题，边界和可解性可继续澄清。

创建前 AR checkpoint 只是不可变执行草稿，不预接纳 ScientificOutcome 或建题。DeepFetch summary 经 RM 接纳后，同一 Session 可修订结果与路线，也可放弃建题完成普通综合。只有决定 create 后才接纳唯一科学结果和修订问题，最后引用真实 accepted QuestionAnchor；failed／cancelled 且无 summary 时按事实决定 retry 或 decline，运行中或等人不算失败。恢复复用已有决定和接纳事实，不重复创建。

## 4. 独立审阅并交接

委派原生独立子智能体检查完整草稿与关键原文，根根据自由格式反馈和自身判断修订最终内容；没有具体问题也可改稿。同根自查不替代独立审阅，Owner 绑定内容 hash 并校验科研来源，不要求审查表单、审阅者身份证明或批准记录。

最终 transition 恰为 `NextCycleProposal | CandidateCompletion` 一项。前者选择已接纳且 present／open 的 Question／Anchor，给出合法入口与精确 skip basis；等待外部条件时明确触发条件，并沿现有 HumanRequest／Owner 路径处理，不用重复 Cycle 轮询同一障碍。后者须有 Quest 整体目标和里程碑依据，并经人类明确确认及 Owner 接纳。

完成条件：科学结果、沉积状态和唯一后继选择清楚，notes 进入正式闭包供下一轮使用；由 RM／RG／AR／AE 接纳链推进。未知结果先对账，必要输入或 currentness 故障保留具体阻塞；仅经公开 Owner 接口操作，不读写私有数据库、spool、seal key 或控制文件绕过边界。
