# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4a.2 生产 import_search/import_register 与 license 来源
- 检查点状态：CP11.4a.1 功能已提交 `43c4134`，build_log 0060 已写；CP11.4a.2 待开工

## 刚完成什么

CP11.4a.1 已闭合“事先登记的 exact action-cycle 候选”消费链：candidate/license 内容哈希不含 SQLite 自增 ID；
plan 必须回引 candidate/license/selection/policy 四锚，提交事务重算；import selection、baseline(planned)、
question_dep(pending)、dependency_wait、释放问题与 cycle 收尾单事务。默认 Advancer 每轮最多处理一个 worker 项；
成功满足 exact dep，终败原子写 failure/baseline build_failed/dep blocked/decision 后回到重规划，崩溃可续 suffix。

独立 plan answerability reviewer 已与 generator 分会话，最多两轮，runner_call/cost/verdict 先耐久，draft/decision/
sidecar exact hash 恢复；第二轮仍 fail 正常 inconclusive。默认 FrozenCandidateFetcher 会限额并校验内容寻址文件、
argv 与供应链闭包，但其 adapter 标为 untrusted；当前没有 adversarial sandbox，所以正式装配必 fail-closed，绝不在
host 裸跑。

## 验证 / Review

- 相关：84/92/105/93 均通过；内部追加的 hash、TOCTOU、非有限 plan、review 身份与基础设施错误回归通过。
- 唯一全量：`7 failed, 1267 passed in 294.51s`；七项均为旧契约锚，更新后 exact `7 passed in 2.96s`；按用户
  要求未二跑全量。
- 外审第 1 轮独立账号 401 无 verdict；第 2 轮 `REQUEST_CHANGES`（3 BLOCKER/2 SHOULD/1 NIT）。四项成立意见
  全修；`plan` 未初始化为误报（try 前已置 None，补回归）；完全相同 candidate tie 已被 DDL 唯一索引排除。
- 当前仍是 operational canary，不是 reference-complete 或 hostile-workload production-ready 系统。

## 当前关键边界

- CP11.4a 父项仍未完成：生产运行没有只读 `repo-search/import_search → import_register` 入口，也没有可回放的
  auto/human license provenance。现在的 import 只对直接 API/受信服务预登记候选可达。
- default adapter 会因缺强沙箱而终败并重规划；这保证不裸跑，但不等于“外部基线已可实用导入”。
- large repo clone/LFS、fd-safe artifact capability、provider invocation/billing exactly-once、container/cgroup/VM、
  跨节点 VEPFS 与含真实外调的 100+ 轮 soak 尚未完成。
- 不得 push；后续仍按用户节奏：开发期相关验证，检查点末尾只一次全量，再提交与记账。

## 下一步动作

1. 先设计受限 import discovery artifact/receipt：触发三闸、query/provider/ranking/pinned revision/search bytes hash，
   模型只能请求只读搜索，不能写 DB 或自报 candidate/license 权威事实。
2. 接生产 `import_search` runner/MCP 或等价受信 connector，编排器短事务 `import_register`，并为零结果/崩溃/
   重启建立 exactly-once marker；license 采用可回放 auto policy 或明确 human decision provenance。
3. 用真实默认装配证明：发现→登记→plan 四锚→dependency_wait 可达；强沙箱仍未落地时物化应诚实 fail-closed，
   不把“可发现/可调度”误报成“已安全执行外部代码”。
