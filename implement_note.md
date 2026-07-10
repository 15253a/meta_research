# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-10 ｜ 位置：步⑪ CP11.2b.3e 真实 connector 入站闭环
- 检查点状态：CP11.2b.3d 已提交；CP11.2b.3e 待开工

## 正在做什么

CP11.2b.3d 功能提交 `6098a69f009f87e24a26a73acd248e3eb4691766` 已闭合真实 connector 的耐久
**出站**投递。Outbox 以 `producer_id + event_key` 作为外部幂等身份，使用耐久 queue/retry/receipt 和严格
ACK 实现 at-least-once 收敛；提供真实 webhook 与固定目标 OneBot v11 outbound transport。调度保持同一
causal lifecycle FIFO，同时具备 urgent 优先、公平轮转、poison isolation、worker 健康上抛和受控退出
backlog 报告。

profile、URL、凭据和网络 deadline 均按 fail-closed 边界收紧；console 只根据严格 receipt 显示 delivered，
损坏 transport authority 显式报错，不再根据本地队列消失伪报送达。相关验证共 `239 passed`；检查点唯一
一次全量为 `1105 passed in 226.11s`，全绿且未二次运行。外审第 1 轮因 401 在 verdict 前结束，第 2 轮运行
10 分钟后超时，均无 verdict，未启动第 3 轮；内部三路复核最终无 BLOCKER。记录见 build_log 0055。

这仍不是完整双向 connector。真实 QQ/web 入站、认证且耐久的 conversation binding、poll/webhook 接入和
“继续”特殊控制语义尚未闭合，因此 CP11.2b.3、CP11.2b 与 CP11.2 仍保持未完成。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- 已提交：CP11.1 `ac53516`；CP11.2a `c077cd2`；CP11.2a.1 `3c4c9b4`；
  CP11.2b.1–2 `10215db`；CP11.2b.3a `7f4fd73`；CP11.2b.3b `2dfa653`；
  CP11.2b.3c `92ecf18`；CP11.2b.3d `6098a69`（对应 build_log 0048–0055）。
- CP11.2b.3d 回退点：`git revert 6098a69`；无 DDL migration，但回退前必须 pause 并备份 connector
  queue/retry/receipt，Git revert 不会清除这些耐久状态。
- 当前仍是 operational canary，不是 reference-complete / 最终 production-ready 系统。

## 下一步动作

1. 审计 reference 的 `Connector.poll/status` 与当前 interaction spool，冻结 CP11.2b.3e 入站身份和 ACK 契约。
2. 实现 webhook/OneBot 入站适配，把认证 source、connector、conversation 和外部 message identity 耐久绑定，
   幂等归一到既有控制面 spool。
3. 闭合“继续”特殊控制语义、入站 cursor/retry、重启恢复、跨会话隔离和 poison message 收敛。
4. 开发期间只跑相关测试；检查点最后只做一次全量，再完成内部复核、最多两轮外审和独立 git 提交。
5. CP11.2b.3e 后进入 CP11.3 单实例锁/heartbeat 与执行边界；随后 CP11.4 和真实 100+ 轮生产验收。

## 关键上下文 / 坑

- `reference/` 是设计权威，不是自动加载的运行时 skill；只有进入 prompt/skill/schema/policy/DDL/代码和反例
  测试的概念才算实现。
- CP11.2b.3d 只有 outbound；不能把 OneBot send 实现描述成完整 QQ connector。
- at-least-once 的最后崩溃窗依赖远端按 `producer_id + event_key` 幂等；部署时必须验证接收端契约。
- inbound conversation id 不能直接信任用户 payload；必须由已认证 connector/source 与外部稳定身份派生并耐久绑定。
- CP11.3 单实例锁尚缺；完成前不得运行两个 writer/orchestrator 实例共享同一 connector 状态目录。
- 真实 SLA、配置 generation 审计、状态日志 rotation/schema version 仍是后续 SHOULD。
- `heartbeat_ref`、CP11.3/CP11.4 和真实 100+ 轮验收未完成，不能宣称系统已与 reference 完整对应。
