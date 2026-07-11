# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4a 默认 import/dependency_wait + 独立 plan 评审
- 检查点状态：CP11.3c 功能已提交 `a32629d`，build_log 0059 已写；CP11.4a 待开工

## 正在做什么

CP11.3c 已把 guardian 的 OS 事实接回 SQLite 业务状态面：plan target `critical/budget_estimate`、严格 current
goal/cycle/question/target lineage、reasoning answer/evidence/tree/selection 原子提交；runner/model/train/eval/smoke
均先建 durable owner 再外调；启动时按 exact receipt 对账，但 exit(0) 绝不伪造指标、评审或 Gate 成功。新增
eval-only 复用既有合法 checkpoint，并用 120 个 attack-intent cycle + 重启证明控制面状态/投影不漂移。

下一检查点 CP11.4a 处理仍属根本缺口的默认 import 路径与 plan 独立评审：当前 `ImportWorker` 虽有显式组件和
owner-first 物化逻辑，但 `run.py` 未装配/驱动，plan `import_defer` 仍被拒；StageProvider 也只有生成产物/schema
重试，没有 reference 所要求的独立 answerability reviewer。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- 已提交：CP11.3c 功能 `a32629d`；build_log 0059、ROADMAP 与本现场快照已随紧邻记账提交入库。
- 唯一全量：`5 failed, 1235 passed in 277.44s`；五项均为有意契约收紧后的旧测试锚，更新后 exact
  `5 passed in 1.59s`；按用户要求未二跑全量。相关批次 78/101/102/80/45/77/59/20/13 均通过。
- 外审已达两轮上限且均无 verdict：第 1 轮独立账号 401；第 2 轮完整 staged diff 内联后耗尽 5/5 WebSocket
  重连并降级 HTTPS 仍 idle。未伪报 APPROVE，详见 build_log 0059。
- 当前仍是 operational canary，不是 reference-complete 或 hostile-workload production-ready 系统。

## 下一步动作

1. 在默认 `build_system/run_system` 装配同 owner fenced `ImportWorker`，生产循环调用
   `materialize_pending()`；先补 restart/idempotency/失败通知，禁止仅测试工厂可用。
2. 接通 plan `import_defer`：候选经 `import_register` 落耐久身份，原子写 dependency_wait route、question dep 与
   cycle 收尾；物化成功后只释放精确问题，崩溃重放不重复 fetch/eval/publish。
3. 将 answerability review 与 plan generator 分离，最多两轮，runner_call/cost/verdict 全耐久；两轮不通过正常
   终结本轮而非楔死。
4. 仍按用户节奏：开发期只跑相关验证，CP11.4a 末尾仅一次全量、功能提交 + build_log 提交。

## 关键上下文 / 坑

- `reference/` 是设计权威，不是运行时自动加载的 skill；当前实现只是逐检查点把概念落实，尚不能称几乎完全
  对应或 production-ready。
- `ImportWorker` 默认未装配，`import_defer` 被拒，是当前最直接的生产链断口；fetch provider 若执行
  git/subprocess 也必须进入 shared supervisor。
- eval-only 当前要求恰一个 checkpoint；hash 校验后子进程仍按路径打开，存在 TOCTOU。content-addressed/fd-safe
  capability、provider invocation/billing exactly-once 留 CP11.4b。
- 当前 backend 不是 cgroup/container：要求 Linux `/proc`、prctl、signal 权限；同 UID hostile workload、
  guardian/receipt 篡改与跨节点 VEPFS 验收留 CP11.4c。120 轮回归只证明控制面长程状态，不代表 120 次真实外调。
- 不得 push；后续每检查点仍须内部审查、最多两轮 codex 外审、仅一次最终全量、功能提交 + build_log 提交。
