# 0055 · CP11.2b.3d 真实 connector 耐久投递

- date: 2026-07-10
- commit: 6098a69f009f87e24a26a73acd248e3eb4691766 — feat: 闭合真实 connector 耐久投递
- branch: fix/architecture-hardening-20260709
- 检查点 / 步: CP11.2b.3d（属：步⑪生产硬化 · CP11.2 人类控制闭环）

## 决策

本检查点闭合 connector **出站**投递，不冒充完整双向 connector。Outbox 事件使用持久
`producer_id + event_key` 作为外部幂等身份，本地以 queue/retry/receipt 三类耐久状态记录待投、退避和回执，
只有远端严格 ACK 后才确认送达。远端已成功、本地 receipt 落盘前崩溃时允许重发；严格 webhook 接收端必须按
该身份耐久去重，使 at-least-once 窗口安全收敛，而不是依赖进程内记忆。

新增真实 stdlib HTTP/HTTPS webhook transport，以及固定 private/group 目标的 OneBot v11 outbound
transport。webhook 严格核协议版本、ACK 身份和结果；OneBot 严格核 echo、retcode 与 message_id，并要求
interaction 的 conversation_id 与 profile 固定目标一致。远端只允许 HTTPS，明文 HTTP 仅允许 literal
loopback；禁止 redirect/proxy，DNS、连接、TLS、响应头和响应体共享总墙钟 deadline。

生产 profile 采用有界、no-follow、regular-file、属主和权限校验；所有 profile（含 loopback）都要求 token，
凭据只从环境变量进入 transport，加载后从随后子进程的继承环境中移除。CLI 在打开 DB 或调用 Codex 前要求
有效 outbound profile，只有显式 `--no-outbound` 才允许离线运行，禁止把未投递静默伪装成生产通知。

Outbox 对 queue/retry/receipt 提供严格 JSON、LF commit/torn-tail 修复、大小上限、跨进程 producer 初始化锁、
原子 producer state 发布、legacy migration、fsync 不确定窗口重核和外部漂移 fail-loud。payload 入队时做
canonical JSON 快照，避免调用方后续修改别名改变耐久事件；receipt/retry 使用缓存，并在 cleanup 崩溃后对账。

调度保持同一 causal lifecycle FIFO，同时为 urgent 保留推进槽、按 priority/group 轮转，使无关 poison event
可以被越过。worker 死亡和异常进入健康状态并由 resident run 主循环上抛；受控退出只尝试一个优先批次，报告
仍耐久保存的 backlog，不被网络或持续新事件无限拖住。console 只根据严格 receipt 显示 delivered；transport
authority 损坏时显式投影 `transport_authority_corrupt`，前端区分 delivered/retrying/corrupt。

## 主要改动

- `orchestrator/connectors.py`：profile loader、真实 webhook/OneBot transport、严格 ACK、总墙钟 deadline、
  因果 FIFO/优先公平调度、worker 健康与有界停止。
- `orchestrator/notify.py`：耐久 producer、queue/retry/receipt、崩溃窗口修复、完整出站事件矩阵和安全 payload。
- `orchestrator/run.py`：显式 outbound 模式、常驻 delivery worker、sideband 探测、退出 drain/backlog 报告。
- `orchestrator/console_server.py`、`views/console/index.html`：诚实 transport authority 投影与投递状态。
- `connectors/README.md`、`outbound.example.json`、根 README 与 interfaces：部署、备份恢复、协议和边界说明。
- `tests/test_connectors.py` 及 notify/run/console 回归：真实 HTTP、严格 ACK、崩溃恢复、公平性、超时、profile
  安全、worker 故障和 console 诚实投影。

## Review

- 内部设计、安全和耐久三路复核发现的 BLOCKER 均已本地修复；最终耐久复核确认 CP11.2b.3d 范围内无
  BLOCKER。保留的 SHOULD：真实外部 query SLA 仍需生产延迟分布验证；receipt 尚未记录配置 generation；
  通用日志 rotation/schema version 尚缺；failure 与 retry state 写入之间崩溃可能少计一次 attempt。
- 外审第 1 轮通过 `bin/codex-review.sh` 启动，但账号 token 返回 401，在 verdict 前结束，没有外部结论。
- 外审第 2 轮按 fallback 模式内联全部 staged diff，运行 10 分钟仍未生成 verdict，按提速要求终止并如实记为
  超时。遵守最多两轮约束，未启动第 3 轮；因此本检查点**没有外部 APPROVE**。

## 验证

- 开发期间仅跑相关验证，共 `239 passed`：`test_connectors.py` 48、`test_notify.py + test_run.py` 128、
  `test_console_server.py` 57、`test_console_frontend.py` 6。
- 提交前 `python -m py_compile`、`git diff --check` 和 `git diff --cached --check` 通过。
- 按用户要求，检查点最后只运行一次全量：`python -m pytest tests/ -q`（workdir=`meta-research/`）→
  **`1105 passed in 226.11s (0:03:46)`**。全绿，未运行第二次全量。
- 结论：**检查点通过**。功能提交未混入 build log/ROADMAP/implement note。

## 遗留 / 回退

- 本检查点只有真实 connector 出站。真实 webhook/OneBot 入站、认证且耐久的 conversation 绑定、poll/webhook
  接入及“继续”特殊控制语义留给 CP11.2b.3e；因此 CP11.2b.3、CP11.2b 和 CP11.2 均保持未完成。
- CP11.3 单实例锁/heartbeat、CP11.4 及真实 100+ 轮生产验收仍未完成，不能宣称 reference 已完整落地或系统已
  无条件 production-ready。
- 代码回退：`git revert 6098a69`。本提交没有 DDL migration；Git revert 不会删除已有 outbox/retry/receipt
  文件。回退前应 pause、备份 connector 状态，并确保回退版本理解现存状态格式。
