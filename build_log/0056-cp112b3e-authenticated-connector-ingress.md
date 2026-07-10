# 0056 · CP11.2b.3e 认证耐久 connector 入站闭环

- date: 2026-07-10
- commit: 505673618ace140af21fd2522dbcffd03621a10e — feat: 闭合认证 connector 入站
- branch: fix/architecture-hardening-20260709
- 检查点 / 步: CP11.2b.3e（属：步⑪生产硬化 · CP11.2 人类控制闭环）

## 决策

本检查点在 CP11.2b.3d 的耐久出站上闭合真实双向 connector，同时保持 run 为唯一 SQLite writer。入站
listener 只在 literal IPv4 loopback 上接收：严格 webhook v1 使用覆盖 key/audience/timestamp/request/event/
原始 body 的 HMAC-SHA256；OneBot v11 reverse HTTP 使用原始 body HMAC-SHA1、`X-Self-ID`、固定目标与 operator
allowlist。provider 只能贡献外部 message identity 和纯文本；connector/profile/source/principal/conversation/
session/idempotency 全由认证 profile 与 transport 元数据派生。

每个 channel 使用隔离 inbox/lock/cursor/retry。transport 只有在记录 LF commit、fsync 后才 ACK；消费通过
non-destructive poll + 严格队首 `commit_poll` 推进，DB 提交前崩溃会安全重放。同一身份同 envelope 返回稳定
duplicate receipt；同身份异文写 hash-only quarantine 并 fail-loud。启动先把从未 ACK 的 torn tail 以 hash/长度/
offset 耐久审计后截断，拒绝把残尾补换行变成 committed poison。slow-drip 从 accept 起共享总 deadline，header
在 stdlib 物化前即有 32 行/16 KiB 上限，handler 槽固定为 16。

常驻轮询每个 channel 每 probe 最多提交一条并轮转起点，本地 console 始终先处理，远端洪泛不能独占紧急
console 同步边界。listener 成功关闭后，退出路径单独把有限 connector backlog 排至空，再终态化已接受 query；
一般 console 仍只做一次最终 probe，独立活 producer 不能无限延迟退出。启动/停止、partial bind、outbound/thread
启动失败都保留并重试实际 ownership，listener handle 只有完全关闭后才释放。

connector 普通文本不能伪造 console action；只有精确 `确认指令 dN` / `拒绝指令 dN` 进入动作语法，并在最终
事务校验同一 connector/conversation/principal/profile。精确“继续”在 message/classification/reply-or-resume
同一事务内冻结到达状态：运行中只回 no-op template，已暂停才创建待确认 hard resume，legacy 半入站不按当前
状态重新解释。interaction/directive 通知绑定来源 channel/conversation；历史缺失可信路由的事件落安全抑制终态，
不会回退默认 QQ 泄漏到另一会话。

## 改动文件

- `meta-research/README.md` — 修改：说明真实双向 connector、来源绑定和历史无路由抑制。
- `meta-research/connectors/README.md` — 修改：新增 webhook/OneBot 入站协议、密钥隔离、耐久状态与恢复运维契约。
- `meta-research/connectors/outbound.example.json` — 修改：加入严格 webhook inbound 示例。
- `meta-research/orchestrator/connector_ingress.py` — 新增：认证 HTTP listener、身份派生、耐久 ACK、poll/commit、恢复与生命周期。
- `meta-research/orchestrator/connector_ingest.py` — 新增：单写进程 connector 消费、公平轮询、严格动作语法与 principal 隔离。
- `meta-research/orchestrator/connectors.py` — 修改：bidirectional adapter/profile loader、公开 inbound 契约与路由校验。
- `meta-research/orchestrator/console.py` — 修改：精确继续原子语义、bounded control JSON 与 source-bound 动作 provenance。
- `meta-research/orchestrator/console_ingest.py` — 修改：可注入 spool/严格失败模式、conversation/session 复核与特殊 query 收口。
- `meta-research/orchestrator/console_server.py` — 修改：动作携带原 conversation，投影安全抑制 delivery 终态。
- `meta-research/orchestrator/console_spool.py` — 修改：抽取 trust-domain 隔离耐久 spool，加入 connector recovery/quarantine。
- `meta-research/orchestrator/interfaces.py` — 修改：冻结 non-destructive poll/commit/lifecycle 的 `DurableInboundConnector` Protocol。
- `meta-research/orchestrator/mediator.py` — 修改：提供 grounded、无模型调用的 continue no-op ACK。
- `meta-research/orchestrator/notify.py` — 修改：interaction 到达先行通知、directive 来源路由、v2 安全抑制回执。
- `meta-research/orchestrator/run.py` — 修改：pre-DB inbound 验证、listener/worker 装配、健康上抛与有限退出排空。
- `meta-research/views/console/index.html` — 修改：前端动作绑定当前 conversation，并显示 suppressed 状态。
- `meta-research/tests/test_connector_ingress.py` — 新增：真实 HTTP、认证/限界/重启/碰撞/公平/退出/隔离回归。
- `meta-research/tests/test_connectors.py` — 修改：路由抑制、directive conversation 与双向装配回归。
- `meta-research/tests/test_console.py` — 修改：运行中/暂停中继续语义与来源动作辅助契约。
- `meta-research/tests/test_console_ingest.py` — 修改：本地 console trust-domain 测试身份对齐。
- `meta-research/tests/test_goal_amend_control.py` — 修改：source-bound action 与 v2 directive event 回归。
- `meta-research/tests/test_notify.py` — 修改：来源通知、v2 生命周期、继续 supersede 语义回归。
- `meta-research/tests/test_run.py` — 修改：有限关闭 backlog drain、装配与阻断文案回归。
- `meta-research/tests/test_runtime_control.py` — 修改：source-bound confirm/reject 与 v2 event 回归。

## Review

- 内部多轮复核先后发现并推动修复：connector torn tail 被后续 append 补成 ACK poison、stop 丢失可重试 handle、
  slow-drip 长占 handler 槽、单 channel 大批吞吐破坏公平、partial start/cleanup ownership 遗失、每 probe 一条后
  最终 drain 只取首条、以及公开接口未覆盖实现实际依赖。最终复核结论为无 BLOCKER/SHOULD；复核侧相关测试
  `166 passed`，`py_compile` 与 `git diff --check` 通过。
- 外审第 1 轮：`codexro-review` 的独立 token/refresh token 已失效，HTTP 401，在 verdict 前结束，无代码意见。
- 外审第 2 轮：用当前凭据把完整 staged diff 内联给 `codex-chatgpt gpt-5.5/xhigh` 只读审查；运行至 480 秒硬
  上限仍未生成 verdict，无代码意见。已遵守两轮上限，未启动第 3 轮；因此本检查点没有外部 APPROVE。
- 未采纳意见：两轮均没有产出意见。

## 验证

- 命令：`pytest -q meta-research/tests/test_connector_ingress.py`（仓库根）
  - 关键输出：`26 passed in 1.46s`。
- 命令：`pytest -q meta-research/tests/test_connectors.py meta-research/tests/test_console_ingest.py meta-research/tests/test_console_server.py meta-research/tests/test_run.py meta-research/tests/test_console.py meta-research/tests/test_notify.py meta-research/tests/test_runtime_control.py meta-research/tests/test_goal_amend_control.py`
  - 关键输出：`344 passed in 83.42s`。
- 退出 drain 与公开接口修复后定向：`pytest -q meta-research/tests/test_connector_ingress.py meta-research/tests/test_run.py::test_drain_exhausts_finite_closed_connector_backlog meta-research/tests/test_run.py::test_drain_does_not_keep_consuming_new_spool_after_boundary`
  - 关键输出：`30 passed in 1.66s`。
- `python -m py_compile`（本检查点修改的 Python 模块）、`python -m json.tool meta-research/connectors/outbound.example.json`、
  `git diff --check` 与 `git diff --cached --check` 均通过。
- 按用户要求，检查点最后只运行一次全量：`python -m pytest tests/ -q`（workdir=`meta-research/`）→
  **`1137 passed in 232.70s (0:03:52)`**。全绿，未运行第二次全量。
- CP11.2 控制闭环验证：真实 HTTP connector 的 fsync ACK、重启去重、cursor drain、跨 principal 动作隔离、
  精确 continue 与来源通知正反例均通过；全量基线无回归。步⑪整体仍待 CP11.3/CP11.4，未宣称步级完成。
- 结论：**检查点通过；CP11.2 人类控制闭环完成**。

## 遗留 / 回退

- CP11.3 单实例锁/heartbeat 与执行进程组边界尚缺；在其完成前，不得让两个 writer/orchestrator 共享同一
  work-root/connector state，也不能把本次单进程恢复性外推成任意生产部署安全。
- OneBot v11 发送没有标准幂等接口，远端成功、本地 receipt 前崩溃仍可能产生用户可见重复；严格 webhook 网关
  才能以 `producer_id + event_key` 机械收敛。OneBot HMAC-SHA1 仅用于线协议兼容，强安全部署宜前置网关。
- acceptance index 目前上限 100,000 identities；状态日志 rotation、配置 generation 审计和真实 SLA 分布仍是
  后续 SHOULD。CP11.4 与真实 100+ 轮生产验收未完成，系统仍是 operational canary。
- 回退：先停止 listener/run，备份 connector inbox/cursor/retry/quarantine/recovery 与 outbox/receipts，再执行
  `git revert 5056736`。本提交无 DDL migration；Git revert 不会删除新耐久文件，旧版本只会忽略 inbound 文件。
