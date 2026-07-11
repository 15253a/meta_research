# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4b 内容寻址与精确调用补账
- 检查点状态：CP11.4a.3 功能已提交 `a31658e`；唯一全量 1322 passed；build_log 0062 已记账；CP11.4a 达成

## 刚完成什么

CP11.4a.3 已闭合另外三类可信来源。`human_named` 只认 hard-confirmed 结构化 directive 的 exact authority；自由文本
URL 不升级权威。`stuck` 机械核 lifetime visit + 当前 goal version 的 append-only inconclusive streak，只普查一次并
原子派生独立参照子题，原题不登记 candidate/import；子题按冻结 authority 在自己的 action-cycle 激活结果。
`sota_reference` 对 allowlisted HTTPS paper/benchmark 做有界读取，原始 bytes 私有写入 SHA-256 内容寻址 blob，再派生
独立 baseline-reference 子题。receipt、runner、candidate/license、policy/cycle 与 child lineage 均可重启交叉核验。

CP11.4a 至此达成：默认 new_structure 发现、四类来源、dependency_wait/worker、独立 plan review 和恢复链已闭合。
默认 materializer 仍因没有敌对沙箱而 fail-closed，因此结论仍是“可可信发现/登记/调度”，不是“可安全执行任意外部代码”。

## 验证 / Review

- 开发期相关组：217、392、130、17 均通过；外审修复后直接相关组合 174 过/1 旧 fixture，修正后 exact 回归全过；
  compile/check 通过。
- 检查点末唯一一次全量：`pytest -q` → `1322 passed in 310.97s`，无失败、无第二次全量。
- 外审第 1 轮独立账号 401，无 verdict；第 2 轮 `REQUEST_CHANGES`（1 BLOCKER/3 SHOULD/1 NIT）。独立 streak、
  receipt 读前路径、真内容寻址、terminalized 诊断和 license cycle/policy 对账已全部修复；两轮上限后未发第 3 轮。
- 功能提交：`a31658e8ddc6a3328ec1b4aba5f860d38b436f5d`；尚未 push。

## 当前关键边界

- CP11.4a 已完成；默认不可信 adapter 仍 fail-closed。large repo/LFS、artifact capability/fd-safe、provider billing exactly-once、
  container/cgroup/VM、跨节点 VEPFS 与含真实外调/失败注入的 100+ 轮 soak 尚未完成。
- 不得 push；继续按用户节奏：开发期只跑相关验证，检查点末只跑一次全量，再提交功能和记账。

## 下一步动作

1. 盘点所有 checkpoint/artifact 的“hash 后按 path 再 open”窗口，设计最小 capability（预开 fd/dirfd + no-follow +
   inode/device/size/hash identity）并覆盖 ImportWorker/manifest/harness 消费链。
2. 给每次真实 provider invocation 建 durable identity，区分“可安全重做的只读调用”与“可能重复计费的调用”。
3. 把 provider usage/billing receipt 与 runner_call/cost ledger 做 exactly-once 对账，明确 unknown usage 的 fail-closed 恢复。
4. 开发期只跑相关验证；检查点边界外审后只跑一次全量，再提交功能和记账。
