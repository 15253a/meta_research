# 四阶段工作方式稿件的独立审计

2026-09-24 补充：以下保留对 9 月 22 日五入口设计的审计结论。其后 [ADR 0006](adr/0006-environment-as-reusable-research-index.md) 将入口扩展为含 Environment 的六入口，Baseline 层级及五个 Owner 保持不变；现成环境登记与新 Target 承担跨机器适配的边界以该 ADR 为准。本次补充不表示 Environment 已实现或经过本篇审计。

日期：2026-09-22。审计由独立子智能体完成，主 Agent 对关键代码位置作了只读核对。对象是 [four-stage-asset-workflow.md](four-stage-asset-workflow.md) 的目标设计及实施歧义，不是全仓代码审计，也不包含测试、部署或运行验证。

## 结论

补清以下实施边界后可作为 GLM 的改造依据。稿件忠实保留了 Idea 的文献方向探索、Reasoning 中 DeepFetch 仅服务拟建新题、独立子智能体 review、大规模资产工作委派、跨 Baseline 自主决策、真实 Run 与可无评价、少副本、五入口和五 Owner 的基本职责。

本轮保持被审计的原稿内容，三项发现及两条范围说明已纳入 [GLM 实施 prompt](glm-8768-implementation-prompt.md)。这些补充说明实现和验收要求，不新增领域对象或科研接纳流程。

## 1. P1：归类纠正需同时保持旧来源可读

稿件第 42、74、236 行描述直接调整关系和内容引用，但未明确当前归属与历史接纳校验如何协调。

当前 [formal_entities.py](../src/meta_research/formal_entities.py) 第 747 行起的 `_register_subject_artifacts` 在第 784–793 行把 `subject_kind`、`subject_ref`、`role` 等纳入产物接纳记录和回执哈希；历史复用也会验证这些记录。GLM 若仅更新已有归属外键，可能使旧交接或复用读取报完整性错误。

实施应通过相应 Owner 维护当前归属并留下简短理由，同时保持内容版本、实际执行事实及旧引用可解析。复用当前必要核验，不靠绕过检查完成移动，不复制正文或补造 Run。验收至少证明“当前入口归属已纠正、旧交接仍可读取同一内容”。

## 2. P1：独立 review 需要替换实际加载的旧相反指令

稿件第 262–266 行的目标明确，但实施必须覆盖真实指令链及必要适配，不能只追加新文案。

目前可见的相反或需协调约定包括：

- [Idea contract](../src/meta_research/skills/idea_stage/references/contract.md) 第 15–17 行：同一根 Session 的第二个 provider turn 自行审查草稿并形成 findings／dispositions。
- [Plan contract](../src/meta_research/skills/plan_stage/references/contract.md) 第 69 行附近：同根 advisory finalization。
- [Reasoning skill](../src/meta_research/skills/reasoning_stage/SKILL.md) 第 42 行：主 Agent 的 Review phase 形成 findings 与处置。
- [Bundle owner operations](../src/meta_research/skills/bundle_stage/references/owner-operations.md) 第 144 行：子智能体不能直接修改最终工作区；需与本轮明确任务范围内的整理／入库委派协调。

独立子智能体负责提出 review 问题，根 Agent 负责改稿。同根后续 turn 可以保留用于修改，但不能冒充独立 review。整理／入库的权限和结果汇集应支持实际委派，权威接纳仍归对应 Owner；不用把任何子智能体变成无限制写入者。退出不再需要的 review 专用填报和接纳要求，不新增 reviewer 资格、审计或接纳协议。

## 3. P2：大规模委派须有可继续查询与读取的工具

稿件第 38–44、264–266 行说明规模和委派，但未把分页和子智能体自行读原件明确写成最小工具行为。若统一工具仍返回全量历史，根 Agent 再转发给子智能体，委派不能减轻原来的上下文负担。

五入口和关系展开应返回有界、可继续分页的摘要及稳定对象／内容引用。子智能体在当前任务范围内自己查询和读取，返回选择、引用及未决项，根 Agent 无需预先收集全库。现有 Baseline 查询已有 `limit/offset` 底座，可延续，不要求新建搜索服务或索引模块。

## 实施范围说明

- “先完成同一批 Target”指第一版不发布未完成 Target 的中间科研产物，不意味着增加整批一次性原子提交。正常文件保存、各 Target 最终接纳和恢复继续沿现有机制工作。
- 当前已有 Baseline／Dataset／Question 查询、显式方法身份、多 Run、无评价 Run、逐 Run 实现版本和产物关联。GLM 应核对并复用，补齐自主跨 Baseline 登记、共用正文读取、完整科研归属及相应 skill 行为，而非重建这些底座。

本审计仍不证明实现已满足稿件。具体接口、可修正关系及适配代码，应由实施 Agent 从当前源码继续核实，并用隔离行为场景验证。
