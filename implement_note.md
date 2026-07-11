# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4a.3 三闸直接/状态来源契约
- 检查点状态：CP11.4a.2 功能已提交 `43dd99e`；唯一全量 1297 passed；build_log 0061 已记账

## 刚完成什么

CP11.4a.2 已把 `new_structure` 的生产发现链接通：plan 只能在本 action-cycle 无候选时单独发一个受限
`import_search_request`；编排器先耐久请求/runner receipt，再做有界 GitHub REST 只读检索，固定 40 位 commit 与
license 内容证据，最后以短事务原子登记 candidate、license、runner/ledger 和 completion decision。重启时有 receipt
只 finalize、不重复 GET；零结果也耐久，重渲染 pack 后换新 plan session。模型不能直接写 DB 或自报 license。

auto license 仅机械允许 Apache-2.0/MIT/BSD-2-Clause/BSD-3-Clause/ISC 的冻结 scope；其他进入 review。新来源使用
v3 snapshot hash，旧 NULL provenance 行保持精确 v2，旧 work-root 可恢复。默认 materializer 仍因没有敌对沙箱而
fail-closed，所以当前是“可发现、可登记、可调度”，不是“可安全执行任意外部代码”。

## 验证 / Review

- 开发期相关组：358、158、20、91 均通过；skills/frozen 8 项通过；compile/check 通过。
- 检查点末唯一一次全量：`pytest -q` → `1297 passed in 301.64s`，无失败、无第二次全量。
- 外审第 1 轮独立账号 401，无 verdict；第 2 轮 `REQUEST_CHANGES`（1 BLOCKER/2 SHOULD/1 NIT）。成立的旧 v2
  hash 兼容和 bool receipt version 已修；DDL 缺失为冻结 migration 事实误报；腐化 finalize 应 fail-loud，未改成
  掩盖审计损坏的普通重规划。两轮上限后不再复审，详见 build_log 0061。
- 功能提交：`43dd99efcdc2469e8f96124695381a6287b19b6f`；尚未 push。

## 当前关键边界

- CP11.4a 父项未完成：`human_named` 尚无结构化 directive 来源，`sota_reference` 尚无冻结 paper/benchmark
  snapshot；`stuck` 目前被正确拒绝直接 search，但“普查→新 idea/question”的耐久来源链还未实现。
- 默认不可信 adapter 仍 fail-closed；large repo/LFS、artifact capability/fd-safe、provider billing exactly-once、
  container/cgroup/VM、跨节点 VEPFS 与含真实外调/失败注入的 100+ 轮 soak 尚未完成。
- 不得 push；继续按用户节奏：开发期只跑相关验证，检查点末只跑一次全量，再提交功能和记账。

## 下一步动作

1. 冻结 `human_named` 的 directive id/version/actor/evidence 与 exact action-cycle 绑定，禁止自由文本暗示直接变权威来源。
2. 为 `sota_reference` 建立内容寻址的 paper/benchmark snapshot 与可回放提取证据，提交时重算。
3. 实现 `stuck` 普查只产新 idea/question 的状态转换；原问题不得直接 search/import，新问题再走既有
   `new_structure` 链，覆盖崩溃恢复与重复消费。
4. 完成相关验证和最多两轮独立外审后，在检查点末只跑一次全量，再提交和记账。
