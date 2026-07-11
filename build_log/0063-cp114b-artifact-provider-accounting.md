# 0063 · CP11.4b artifact capability 与 provider 精确补账

- date: 2026-07-11
- commit: `e236487e31c4f30de1ae2d344afc4100e8faebca` — feat: close CP11.4b artifact and provider accounting
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4b（属：步⑪ CP11.4 残余架构边界；CP11.4c 继续）

## 决策

本检查点闭合两类此前会破坏长程可恢复性的系统边界：文件身份不再只是“先按路径 hash、随后再按路径打开”，每次真实
provider 调用也不再只依赖进程内返回值记账。前者改为可复核的 fd capability，后者改为 guardian capture、provider
invocation receipt、runner_call 与 cost ledger 的耐久对账。

- source tree 从稳定 root dirfd 相对遍历，普通 artifact/checkpoint 以 `O_NOFOLLOW` 打开后，在同一 fd 上取得
  device/inode/size/hash 身份和读取内容；消费者通过 `/proc/self/fd/<n>` 与 `pass_fds` 使用已验证对象，并在使用后复核
  capability、持久路径绑定或整棵 source tree。路径替换、symlink、内容变化和 fd 身份变化均 fail-closed。
- manifest、attack stage、harness 与 import worker 共用 capability，而不各自重复 path/hash/open。checkpoint 注册阶段持有
  capability 并核对最终持久路径；import 的字符串内容先规范成 bytes，避免类型分支绕开身份核验。
- 所有 Codex 调用统一请求 JSON 事件；本地 guardian operation 给出必有的 invocation identity，provider 若返回
  thread/session id 则额外绑定。receipt 严格绑定 prompt、model、effort、execution、usage 与捕获证据；冲突或无法解析的
  usage 进入 unknown，不能按零成本继续。
- supervisor 将 stdout/stderr 写入 0600 capture，terminal guardian receipt 锚定 inode/size/hash。owner 在 provider 返回后、
  写 provider receipt 或 DB 前死亡时，reconciler 可只依 guardian capture 重建 receipt；capture 本身失败会落终态
  `capture_error`，不会让 running 调用永久卡住。
- provider receipt、cost ledger 与 `provider_invocation_accounted` 在同一个 WriteDaemon 管理的事务中提交。冻结 Appendix-A
  schema 不新增 unique index，因此用单写/RLock/`BEGIN IMMEDIATE` 串行边界并机械断言当前活跃事务，保证一个 invocation
  只进入本地账本一次。regular/query 调用恢复时只补调用终态与账，不伪造业务成功。
- 这里的 exactly-once 仅指“每个已观察到的 invocation 在本地恰好记账一次”，不声称 provider 侧执行恰好一次；金额是
  policy projection，明确不是供应商 invoice。嵌套目录下同 UID 并发篡改、强敌对隔离和跨节点 receipt 信任仍属 CP11.4c。

## 改动文件

- `meta-research/README.md` — 记录 fd capability、provider 本地补账语义及 CP11.4c 尚未覆盖的诚实边界。
- `meta-research/orchestrator/artifact_capability.py` — 新增单 fd 身份/读取、稳定 dirfd source-tree 遍历、路径绑定与使用后复核。
- `meta-research/orchestrator/attack_stages.py` — checkpoint/source 以 capability 交付并在外部使用后复核；传播 invocation receipt。
- `meta-research/orchestrator/cost_ledger.py` — provider invocation 与本地 policy cost 单事务 exactly-once 入账，unknown fail-closed。
- `meta-research/orchestrator/execution_reconcile.py` — 从 guardian capture 重建 provider receipt，恢复 regular 调用并只补账不造业务成功。
- `meta-research/orchestrator/harness.py` — 通过预开 fd 执行已验证 artifact。
- `meta-research/orchestrator/import_worker.py` — import source/content/checkpoint 统一 capability 消费与使用后身份复核。
- `meta-research/orchestrator/interfaces.py` — artifact/调用结果接口携带 provider invocation receipt 引用。
- `meta-research/orchestrator/manifest.py` — `{src}`/checkpoint 改用 dirfd/fd，绑定预期 source ledger 与 checkpoint hash。
- `meta-research/orchestrator/mediator.py` — query provider 调用同样执行 receipt 恢复和精确补账。
- `meta-research/orchestrator/process_supervisor.py` — 0600 stdout/stderr capture、terminal capture identity 与 `capture_error` 终态。
- `meta-research/orchestrator/provider_invocation.py` — 新增严格 `provider-invocation-v1` receipt、重建、验证和 invocation identity。
- `meta-research/orchestrator/run.py` — 将 provider accounting/reconcile 装入生产入口。
- `meta-research/orchestrator/runner.py` — 所有 Codex 调用使用 JSON 事件，解析 usage/thread/session 并生成 receipt。
- `meta-research/orchestrator/stage_provider.py` — 各阶段结果传播 provider receipt 引用。
- `meta-research/tests/test_artifact_capability.py` — 覆盖 fd/path/tree 身份和篡改拒绝。
- `meta-research/tests/test_execution_reconcile.py` — 覆盖 capture 重建、owner-death、幂等补账与篡改拒绝。
- `meta-research/tests/test_manifest.py` — 覆盖 source/checkpoint path swap 与 fd 交付。
- `meta-research/tests/test_query_responder.py` — 覆盖 query 调用 receipt 恢复和一次入账。
- `meta-research/tests/test_runner_usage.py` — 覆盖 JSON usage/identity、冲突 unknown 与 capture 边界。

## Review（codex-chatgpt gpt-5.5/xhigh）

- 第 1 轮：`codexro-review` 在产出 verdict 前因独立账号 401 `token_invalidated` 失败；没有把基础设施失败记为批准。
- 第 2 轮（上限）：完整 staged diff 内联只读审查，输出 `/tmp/cp114b-review-r2.md`，结论
  `REQUEST_CHANGES`。逐项核实如下：
  - BLOCKER（采纳）：capture identity 写入失败会让 guardian 永远保持 running。新增 terminal `capture_error`，恢复时 usage
    unknown 并 fail-closed，不再无限轮询。
  - BLOCKER（不成立）：认为 specialized `interaction_query`/`import_search` 会被统一 reconciler 遗留 running。实际
    Mediator 与 ImportSearchService 独立扫描数据库，进程内 `seen` 不共享；补充架构注释，但不引入重复 owner。
  - BLOCKER（按冻结架构不采纳）：要求新增 DB unique index。Appendix-A DDL 已冻结，且所有写均经唯一 WriteDaemon 的
    RLock/`BEGIN IMMEDIATE`；增加 active-transaction 机械断言，使已有串行唯一性前提可验证。
  - SHOULD（采纳）：README 对 nested dirfd 边界表述过强，收窄为当前真实保证。
  - SHOULD（采纳）：artifact payload 读取前显式 `lseek(0)`；import `str` content 先转 bytes。
  - NIT：`int()` 本身接受外围空白，未为纯风格建议制造行为变更。
- 已到两轮上限，不发第 3 轮；所有成立项在功能提交前修复并做相关回归。

## 验证

- 开发期只做相关验证：runner usage **32 passed**；process supervisor **22 passed**；execution/reconcile **36 passed**；
  cost/query **76 passed**；artifact/import/manifest **87 passed**；run **47 passed**；attack advance **76 passed**；
  manifest/import/stage **123 passed**；新增 guardian capture 重建确认 222 tokens 只记一次。
- `compileall`、`git diff --check` 与 staged diff check 均通过。
- 提交边界唯一一次全量：`pytest -q` → **`1332 passed in 313.81s (0:05:13)`**；无失败、没有第二次全量。
- CP11.4b 验收结论：外部进程消费的 source/checkpoint/import artifact 已有 fd-safe 身份链；regular/query provider 调用可在
  owner-death 缝隙从 capture 恢复，并与 runner_call/本地 cost ledger 一次对账。该结论不包含供应商侧 exactly-once 或
  invoice，也不包含强敌对沙箱、receipt 跨信任域防篡改、VEPFS 跨节点与真实 100+ 轮 soak。

## 遗留 / 回退

- CP11.4c：container/cgroup/VM 或等价强敌对隔离；guardian/capture/receipt 跨信任域防篡改；VEPFS 跨节点 owner/lease/
  fd 语义验收；含真实 provider 外调和故障注入的 100+ 轮 soak。CP11.3c 的 120 轮只证明控制面状态稳定，不能替代它。
- 回退前停止 orchestrator，保留 DB、guardian/provider receipts、capture 与 cost ledger 证据；执行
  `git revert e236487e31c4f30de1ae2d344afc4100e8faebca`。本提交无 DDL migration，但旧代码不理解
  `provider-invocation-v1`、capture identity 或 capability 后验核验，不应在 invocation/execution 在途时热回退。
