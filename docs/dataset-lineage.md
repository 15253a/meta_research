# 精确数据来源与派生关系

Dataset 表达可复用的数据含义，DatasetVersion 绑定 RM 已接纳的精确资产版本，DatasetReference 表达研究用途。DatasetDerivation 只补充“哪些数据经过什么处理得到哪些数据”的关系；数据文件、版本、内容 hash 和保存方式仍由 RM 管理。

## 研究者决定粒度

例如原始访谈、脱敏文本、编码表可以分别保留为有研究意义的数据版本，并记录原始访谈 → 脱敏文本 → 编码表。也可以只保留原始数据和最终编码表，在一条派生关系中说明中间处理。Agent 决定哪些中间版本值得保留，不要求逐文件、逐步骤登记。一次合并有两个来源时，对同一派生版本登记两条边。

登记关系不复制数据、不改变已有内容、不表示某项处理已被独立复核。原始外部来源、采集方法、授权/使用限制、处理脚本或方法、数据切分依据和可重建条件，应按实际情况写入已有的 meaning、metadata、notes 或 processing；不增加固定领域表单。

## 使用现有 Owner 工具

1. 用 `research_graph.datasets.page` 搜索已有全局 Dataset；复用正确身份，必要时用 `register` 建立新的语义身份。
2. 用 `register_version` 绑定已接纳 RM 资产的精确 `asset_ref`、`version_ref`、content/manifest hash 与 receipt。它不是文件上传接口，不自动复制或备份数据。
3. 用 `research_graph.datasets.derive` 登记来源到派生版本的一条有向关系。参数为 `effect_id`、`source_dataset_version_ref`、`derived_dataset_version_ref`、`question_ref` 和自由文本 `processing`，可附 `research_ref`、`notes`。每条边都有精确引用、内容 hash 与接纳 receipt。
4. 用原有 `reference` 记录某个 Question 为什么使用该精确版本；可附已有的 Baseline、Variant、VariantRun、Evaluation、EvaluationAttempt、Target 或 TargetCommit 引用。`research_ref` 指向相关研究出处或复用依据，允许引用其他 Quest 的精确历史工作，不代表该对象在当前 Quest 生产了数据。

来源和派生版本都须已经接纳。数据身份和相关研究引用可跨 Quest 合法复用，但当前写入的 Question 必须属于已认证 Root 的 Quest；不存在的研究引用会被拒绝。实际数据来源由精确版本关系和处理说明表达，相关研究引用不替代生产或验证证据。自指和来源环会被拒绝，避免把后来的数据写成自身的原始来源。丢失工具回复后用相同 `effect_id` 调用 `derive.reconcile`；同一逻辑 Root 经技术恢复后仍可找到原结果，不重复建边。

## 默认读取有限，按需展开

`page(dataset_version_ref=..., direction="sources")` 查看该版本的直接来源；`direction="derived"` 查看直接下游；默认 `both`。使用 `offset`、`limit` 继续分页，每页最多 100 条，不自动展开整个历史图。

`read` 一次提供一个精确引用：`dataset_ref`、`dataset_version_ref`、`dataset_reference_ref` 或 `dataset_derivation_ref`。返回完整记录、hash 和 receipt；沿所需版本继续读取上游或下游。既有 `page(question_ref=...)` 仍列出该问题的数据用途引用。

## 新数据何时登记

所有 Root 都可登记已经接纳的版本。当前 Root MCP 没有通用 RM 文件导入工具；Target 的普通新产物先经过 completion 接纳，之后 Bundle 或 Reasoning 可用精确 RM bindings 登记 DatasetVersion、来源关系和用途。已接纳的输入数据无需重新导入。

大型或昂贵数据应保存在稳定持久位置，避免覆盖唯一原始来源；把路径、来源和当前保存状态交接清楚。正式接入新资产时使用现有资产入口或请求人类协助。RM 已有 `linked_local` 与异步流式校验能力，但链接不等于备份。派生登记不会绕过 Target 交接包容量限制，也不以登记完成来假定数据已复制、始终在线或已经完成科学评价。
