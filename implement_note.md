# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-10 ｜ 位置：步⑪ CP11.3 状态与执行边界
- 检查点状态：CP11.2b.3e 已提交并完成记账；CP11.2 人类控制闭环完成

## 正在做什么

CP11.2b.3e 功能提交 `505673618ace140af21fd2522dbcffd03621a10e` 已闭合真实 connector **入站**：
webhook HMAC-SHA256 与 OneBot reverse HTTP HMAC-SHA1 只在 IPv4 loopback 接收，服务端派生
connector/profile/source/principal/conversation/session/idempotency，先 fsync channel 隔离 spool 再返回
transport ACK。消费端采用 non-destructive poll + 显式 durable commit，retry、identity collision quarantine
和未 ACK crash-tail recovery 都是独立耐久 authority。

普通文本不能进入 console 结构化动作域；connector 只接受精确 `确认指令 dN` / `拒绝指令 dN`，并在最终
事务复核 connector/conversation/principal/profile。精确“继续”在一笔事务内按到达时 pause 状态解释：运行中
只回无状态变更 ACK，已暂停才建立待确认 resume。通知回原 source channel/conversation；升级前无可信路由的
旧 interaction/directive 事件被安全抑制，不回退到默认 QQ。常驻每通道每 probe 一条保持公平，listener 成功
关闭后退出路径把有限 connector backlog 排至空，再终态化全部已接纳 query。

内部最终复核无 BLOCKER/SHOULD；相关回归 `344 passed`，退出/接口定向 `30 passed`，复核侧 `166 passed`。
检查点唯一全量为 `1137 passed in 232.70s`，全绿且未二次运行。外审第 1 轮因 codexro 凭据 401 无 verdict，
第 2 轮完整 staged diff 内联审查至硬上限仍无 verdict；已用满两轮，没有第 3 轮。记录见 build_log 0056。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- 已提交：CP11.1 `ac53516`；CP11.2a `c077cd2`；CP11.2a.1 `3c4c9b4`；
  CP11.2b.1–2 `10215db`；CP11.2b.3a `7f4fd73`；CP11.2b.3b `2dfa653`；
  CP11.2b.3c `92ecf18`；CP11.2b.3d `6098a69`；CP11.2b.3e `5056736`（build_log 0048–0056）。
- CP11.2b.3e 回退点：`git revert 5056736`；无 DDL migration。回退前须停止 listener/run 并备份
  connector inbox/cursor/retry/quarantine/recovery 以及 outbox/receipt，Git revert 不会删除这些耐久文件。
- 当前仍是 operational canary，不是 reference-complete / 最终 production-ready 系统。

## 下一步动作

1. 对照 reference 审计并切分 CP11.3：critical/budget_estimate 落库与早退、goal 最新版、orchestrator
   单实例锁/heartbeat、超时终止进程组。
2. 优先闭合共享 work-root 的单实例所有权，再做执行超时/进程组恢复，避免真实长跑出现双 writer/双 listener。
3. 开发期间继续只跑相关测试；每个检查点边界最多两轮外审，最后只做一次全量并独立提交/记账。
4. CP11.3 后进入 CP11.4 残余架构边界，再做真实 100+ 轮生产验收。

## 关键上下文 / 坑

- `reference/` 是设计权威，不是自动加载的运行时 skill；只有进入 prompt/skill/schema/policy/DDL/代码和反例
  测试的概念才算实现。
- webhook 网关要按 `(producer_id,event_key)` 耐久去重；OneBot v11 没有标准幂等发送接口，极窄崩溃窗仍可能
  产生用户可见重复。OneBot HMAC-SHA1 是兼容线协议，强安全部署宜在前面放严格 webhook 网关。
- connector acceptance index 当前有 100,000 身份上限；状态日志 rotation/schema generation 审计仍是后续 SHOULD。
- CP11.3 单实例锁尚缺；完成前不得运行两个 writer/orchestrator 实例共享同一 work-root 或 connector 状态目录。
- `heartbeat_ref`、CP11.3/CP11.4 和真实 100+ 轮验收未完成，不能宣称系统已与 reference 完整对应或可无条件
  支持上百轮生产执行。
