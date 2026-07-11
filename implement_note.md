# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.3b 执行 owner-death 边界
- 检查点状态：CP11.3a 已提交 `d79d3d0`（build_log 0057）；CP11.3b 待开工

## 正在做什么

CP11.3a 已把共享 work-root 的“单 writer”闭合为进程级 owner capability：稳定非阻塞 flock 是唯一权威，
metadata/heartbeat 只用于诚实观测；WriteDaemon、Runner、manifest spawn、connector 入出站与 System lifecycle
均受 owner guard，所有 worker/DB 能力消失后才 lock-last 释放。fork child、构造回滚、SIGKILL takeover、
`KeyboardInterrupt` descriptor release 与 stale cursor/observer 竞态均有反例。

下一检查点 CP11.3b 将处理尚未闭合的根本执行边界：当前 regular Codex/manifest/import 的 timeout 仍可能只终止
直接子进程，daemonize/派生子孙可在旧 owner 死亡后继续写 staging。要统一 process group/session（部署允许时再
评估 cgroup）、owner-death kill、整组 join 与 durable timeout terminal receipt，之后才能把 takeover 称为
crash-safe production recovery。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- 已提交：CP11.1 `ac53516`；CP11.2a `c077cd2`；CP11.2a.1 `3c4c9b4`；
  CP11.2b.1–2 `10215db`；CP11.2b.3a `7f4fd73`；CP11.2b.3b `2dfa653`；
  CP11.2b.3c `92ecf18`；CP11.2b.3d `6098a69`；CP11.2b.3e `5056736`；
  CP11.3a `d79d3d0`（build_log 0048–0057）。
- CP11.3a 唯一全量：`1180 passed, 1 failed`；失败是测试全局 thread 假设，改为跟踪本用例 thread 后定向
  `38 passed in 2.02s`。按用户要求未运行第二次全量，记录未伪装成全量全绿。
- CP11.3a 回退：停止所有 orchestrator/listener/worker，备份 work-root 后 `git revert d79d3d0`；无 DDL migration。
- 当前仍是 operational canary，不是 reference-complete / 最终 production-ready 系统。

## 下一步动作

1. 审计 `runner.py`、`manifest.py`、import/harness 的全部 subprocess 入口、timeout/信号/日志/receipt 状态机与
   当前测试可注入边界，切出 CP11.3b 最小统一 supervisor 契约。
2. 先用相关故障注入验证子进程派生孙进程、超时 TERM→KILL、owner SIGKILL/restart 与 staging fence；仍按用户
   要求开发期只跑相关测试，检查点外审收敛后只跑一次全量。
3. 随后进入 CP11.3c：target critical/budget_estimate、goal lineage、runner heartbeat/timeout receipt；再做
   CP11.4 与真实 100+ 轮生产验收。

## 关键上下文 / 坑

- `reference/` 是设计权威，不是自动加载的运行时 skill；只有进入 prompt/skill/schema/policy/DDL/代码和反例
  测试的概念才算实现。
- CP11.3a 只保证同机内核 flock 语义；跨节点共享 VEPFS 必须在实际两节点和挂载参数上做同时 acquire 验收。
- owner SIGKILL 后旧的未受管子孙仍可能存活；CP11.3b 前只能执行受信任、不会 daemonize 的 manifest 命令。
- 手工构造 `System` 的 accepted-only callback 默认/owner guard 仍有泛化 SHOULD；生产 `build_system()` 已显式
  注入 `mediator.poll/has_pending_queries`，DB/Runner 内层 owner guard 已覆盖。
- status card 的 responder `heartbeat_ref` 仍为 `None`；instance heartbeat 只服务 owner 活性，不能冒充当前
  LLM response heartbeat。
- target critical/budget_estimate、严格 goal lineage、durable timeout receipt、CP11.4 和真实 100+ 轮验收尚未
  完成，不能宣称系统已与 reference 完整对应或可无条件支持上百轮生产执行。
