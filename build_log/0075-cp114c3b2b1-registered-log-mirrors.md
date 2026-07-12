# 0075 · CP11.4c.3b.2b.1 registered execution-log mirror

- date: 2026-07-12
- commit: `f59c72cabcbbaa4ec377aaf84cb869082d4cbf3b` — feat: add CP11.4c.3b.2b.1 registered log mirrors
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3b.2b.1（属：步⑪ CP11.4c.3b 存储治理）

## 决策

只为最新已深验 SQLite snapshot 中身份完整的 `execution_log` 行建离线压缩镜像：
SQLite 仍是结构化真相，冻结 ref/hash/bytes 原件仍保留且不变。不 glob runner transcript、
guardian capture、sandbox session 或未入库失败日志，也不新建 daemon、第二 DB、scheduler 或自动 GC。

相关验证在当前 VEPFS 上暴露出旧 b.2a restore 的 `renameat2(RENAME_NOREPLACE)` 对目录返回
`EINVAL`。保留支持 flags 文件系统的原子快路；VEPFS fallback 在目标出现前先耐久化 sibling
parent claim，再以 exact token 获取目标 instance lease、发布 inner ready marker。完成时仍持 lease，
先解 parent claim、后解 inner marker；任一中断点至少留一道启动拒绝门。

## 改动文件

- `meta-research/orchestrator/storage_assets.py` — 新增：DB-registered log 枚举、deterministic gzip CAS、
  immutable per-row index、容量门、有界解压验证和 object/index 崩溃重放耐久封口。
- `meta-research/orchestrator/storage_ops.py` — 修改：增加 `mirror-logs` / `verify-log-mirrors` CLI；
  restore 增加 VEPFS lease-fenced publication fallback 与诚实 receipt 字段。
- `meta-research/orchestrator/instance_lease.py` — 修改：普通启动 fail-closed 拒绝 restore parent/inner
  marker，仅 exact restore claim token 可在 fallback 发布期取 lease。
- `meta-research/tests/test_storage_ops.py` — 修改：覆盖空/二进制日志、原件漂移、同 fd DB 根、
  gzip 多 member、CAS/index 耐久窗口、容量拒绝、CLI 及 VEPFS fallback 双 marker 崩溃窗口。
- `meta-research/README.md` — 修改：固定离线命令、scope、复杂度边界、恢复限制与 VEPFS 运维语义。
- `ROADMAP.md` — 修改：将 b.2b 拆成已完成的 b.2b.1 与待完成的 import/dependency b.2b.2。

## Review

- 内部 registered-log 终审 `APPROVE`：确认 snapshot DB 查询 fd 重核 manifest hash/bytes，源日志
  同 fd hash+压缩，gzip 输出有界，object/index rename→fsync 窗口可重放封口。
- 内部 restore 审查首轮找到“target mkdir 到 inner marker”的 fail-open 窗口和 parent fsync 缺口；
  加入 durable sibling claim、exact token 与双 marker 交接后终审 `APPROVE`。
- 外审第 1 轮：`codexro` 凭证失效，HTTP 401，无 verdict。
- 外审最终第 2 轮：root 账户只读全内联模式接收完整 staged diff，240 秒内仅返回
  开始审查消息，无问题或 verdict；按两轮上限终止，未发第 3 轮。

## 验证

- 命令：`python -m pytest tests/test_storage_ops.py -q --basetemp=<VEPFS-unique-temp>`
  - 关键输出：`30 passed in 72.58s`。
- 命令：`python -m pytest tests/test_instance_lease.py -q --basetemp=<VEPFS-unique-temp>`
  - 关键输出：`40 passed in 4.54s`。
- 真文件系统能力探针：当前 VEPFS 的 `RENAME_NOREPLACE` / `RENAME_EXCHANGE` 均返回
  `EINVAL`，`/tmp` 均成功；因此相关测试在 VEPFS basetemp 上真走 fallback。
- `python -m py_compile` 相关四文件、`git diff --check` 与 staged diff check 均通过。
- 结论：相关 **70 passed**。未跑全量：遵守用户“中间只跑相关验证，最后只做一次全量”的要求；
  当时 overlay 约余 54MB，也小于已知全量所需约 191MB。

## 遗留 / 回退

- b.2b.2 仍须闭合 repository materialization index/object 以及 v3 dependency-image 传递 CAS；完成前
  b.2/b 整体不验收，当前 `restore` 仍是 `sqlite_truth_only`。
- log mirror CLI 是显式离线检查点操作，不应每轮做历史全量扫描；原件和 orphan CAS 都不由它删除。
- fallback 中途失败会故意保留 sibling/inner marker 与部分目标，需操作员检查后再清理；
  真 SIGKILL/双节点掉电 canary 留给 CP11.4c.3c.2。
- 无 DDL。代码回退：`git revert f59c72cabcbbaa4ec377aaf84cb869082d4cbf3b`。回退不会自动删除已发布的
  log-mirror CAS/index 或人工待检查的 restore marker。
