# 记忆系统设计工作区

本轮整理整套记忆的入库、存储、索引、发现与采用。实际研究产物提供设计反馈；先建立决策地图，再与研究负责人逐项讨论。

- [现状与决策依赖](memory-system-map.md)
- [资产与写入证据](asset-inventory.md)
- [索引与一致性证据](index-inventory.md)
- [当前运行反馈](runtime-memory-feedback.md)
- [基线验证与两项已知失败](baseline-verification.md)

GitHub 地图：[记忆系统设计地图：入库、存储、索引与使用（8768 → v1）](https://github.com/15253a/meta_research/issues/148)。首张待讨论票：[记忆对象、身份和版本：哪些是事实，哪些是派生内容？](https://github.com/15253a/meta_research/issues/149)。地图与原生子 issue 是后续决策状态的权威入口；这些文档记录 2026-09-24 的调查证据，不作为另一套待办状态。

## Wayfinding operations

地图使用 `wayfinder:map` 标签；所有本轮决策票使用 `wayfinder:grilling`，通过 GitHub 原生 sub-issue 归属地图，使用原生 blocked-by 关系表达依赖。前沿是开放、未分配且所有阻塞票已关闭的子 issue。

工作某张票前先分配给负责的开发者；通过真实人机讨论决定其问题，结论写 resolution comment，再关闭并把名称链接加入地图 Decisions so far。每轮最多解决一张非 research 决策票。遵循 wayfinder、grilling 与 domain-modeling；已确认的术语再写入 CONTEXT.md，未讨论的提议不冒充既定合同。

用户明确选择：本轮只完成现状、问题和地图；当前 8768 是保存基线。八张旧开放 issue 在完整备份后按用户指令永久删除，新地图接续本轮工作；删除不表示历史验收通过。已关闭的历史决定继续保留作可追溯背景，不自动增加本轮门禁。
