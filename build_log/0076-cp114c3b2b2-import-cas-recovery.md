# 0076 · CP11.4c.3b.2b.2 import materialization CAS recovery

- date: 2026-07-12
- commit: `d72da353aefa8dc4f7b437ee08b2e197251fd7ff` — feat: add CP11.4c.3b.2b.2 import CAS recovery
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3b.2b.2（属：步⑪ CP11.4c.3b 存储治理）

## 决策

在现有 immutable SQLite snapshot 与既有 repository/dependency CAS 上补 import 灾备闭包；不加 daemon、
第二 DB、scheduler、运行时状态机或自动 object GC。所选 backup 中的 `build_target` 不能只凭任意同-object
index 恢复：必须沿 `import_worker_cycle → selected_for_materialization → external_candidate` 重建 exact candidate
index，再把 `plan_ref.repository_snapshot_hash` 闭合到 repository object，并传递闭合 v3 dependency-image。

离线 verifier 不得读取当前 policy、调 Docker/网络或重新 resolve image，因此从既有 live verifier 抽出纯文件
inspector；live wrapper 仍补当前 config/environment/allowlist/compiler/Docker resolve。恢复以一步组合命令为推荐入口：
SQLite target 第一次可见时已带 exact continuation marker，随后在同一 source lease 内完成 dependency → repository
→ canonical index → completion receipt。VEPFS fallback claim 由 frozen restore receipt + resolved target 确定性绑定，
已有 claim/marker/SQLite/receipt 均按 exact bytes 重放，不另造 journal。

## 改动文件

- `meta-research/orchestrator/dependency_image_inspector.py` — 新增：policy-independent dependency-image receipt、
  lock/wheelhouse/install/runtime/context/archive 全闭包核验及 capability 重建。
- `dependency_image_store.py`、`dependency_image_runtime.py`、`dependency_image_common.py` — 修改：live verifier
  复用纯核并保留 current-policy 闸；symlink root 在 walk 前拒绝，current archive 先限额，长 hash 续 owner guard。
- `repository_materializer_store.py` — 修改：新增 repository object/index 纯 inspector；v2 base environment、v3
  execution capability、adapter/ledger/tree/index canonical identity 闭合；live wrapper 保留 current policy/resolve。
- `import_materialization_contract.py`、`import_worker.py` — 新增/修改：共享历史等价的 plan_ref/ledger/contract
  投影，避免 storage 复制第二种 DB identity 算法。
- `storage_imports.py` — 新增：immutable backup DB 根、exact candidate index、repository/dependency 去重闭包，
  orphan 报告、target block/inode capacity、组合恢复、reuse fsync 与 object/index/receipt 崩溃重放。
- `storage_ops.py`、`instance_lease.py` — 修改：新增 verify/restore/combined CLI；exact marker 特权续接；
  atomic 与 VEPFS fallback 首次可见启动栅栏；O_TMPFILE+linkat 完整 claim 发布及 source-bound fallback replay。
- `test_dependency_image.py`、`test_repository_materializer.py`、`test_storage_imports.py`、`test_storage_ops.py` —
  修改/新增：纯核、policy 独立性、篡改、100 target 去重、plan downgrade/index alias、base environment、容量、
  CLI 与 claim/marker/SQLite/object/index/receipt 故障窗口。
- `meta-research/README.md` — 修改：固定离线命令、scope、组合恢复流程与非完整 DR 边界。

## Review

- 内部 dependency/repository 终审首轮发现 dependency symlink root 在旧 current-policy walk 前未先拒绝，及 v2/v3
  base environment 离线传递缺口；修复 root lstat、v2 local bind、v3 repo→dependency receipt bind 与 archive
  fail-fast/guard 后最终 `APPROVE`。
- 内部 DB lineage 审查实证两项 BLOCKER：新式 plan_ref 删除 root 可静默降级 legacy；删除 exact candidate index、
  放入同-object alias index仍可通过。改为 exact shape + worker selection lineage 后最终 `APPROVE`。
- 内部 storage 恢复审查先后实证 command gap、claim 半写、atomic parent fsync、VEPFS fallback early crash、reuse
  durability、index TOCTOU 与小文件/目录项容量低估；均以 existing lease/marker、source-bound claim、canonical frozen
  index、durable confirm 与 block budget 局部修复，未引入新服务/状态机。三类 SQLite fallback 及三类 import CAS
  故障回归后最终 `APPROVE`。
- 外审第 1 轮：`codexro-review` 独立凭证已失效，HTTP 401，无 verdict。
- 外审最终第 2 轮：root 账户只读内联模式收到完整 staged diff；本机 bwrap namespace 不可用，模型尝试的只读
  shell 均失败，约 5 分钟、`49,093` tokens 后仍无问题或 verdict，主动终止。已到两轮上限，未发第 3 轮。

## 验证

- `pytest meta-research/tests/test_storage_imports.py -q --basetemp=<VEPFS-unique-temp>`
  - 关键输出：`14 passed in 35.47s`；含 100 DB roots 去重、exact index/plan/base-env、容量、CLI，
    claim/marker/SQLite receipt 与 object/index/completion receipt 六类故障窗口。
- `pytest meta-research/tests/test_storage_ops.py -q -k '<四项 restore 回归>' --basetemp=<VEPFS-unique-temp>`
  - 关键输出：`4 passed, 26 deselected in 10.30s`。
- `pytest meta-research/tests/test_dependency_image.py -q -k dependency_image_file_inspector ...`
  - 关键输出：`5 passed, 11 deselected in 0.19s`。
- `pytest meta-research/tests/test_repository_materializer.py -q -k '<offline/runtime selections>' ...`
  - 关键输出：`5 passed, 39 deselected in 0.68s`。
- `pytest meta-research/tests/test_import_worker.py -q -k '<materialize/repository/dependency selections>' ...`
  - 关键输出：`3 passed, 24 deselected in 8.11s`。
- 当前 VEPFS 实测 `O_TMPFILE` open + `/proc/self/fd` `linkat(AT_SYMLINK_FOLLOW)` 成功；组合 CLI 真走
  `renameat2` flags 不支持时的 fallback。相关 `py_compile`、`git diff --check`、staged diff check 均通过。
- 结论：相关 **31 passed**。未跑全量：遵守用户“实现期只跑相关验证，最后检查点才做一次全量”的要求；
  当前 20G overlay 也仅约余几十 MiB，已知全量 basetemp 约需 191MiB。

## 遗留 / 回退

- 本检查点只闭合 SQLite-registered repository/dependency CAS。execution-log 正本、checkpoint/content store、
  views Git、完整 work-root 与跨 fileset/站点 DR 不在 scope，故 CP11.4c.3b/b.2/b.2b 父项仍不勾选。
- legacy embedded import 与 unbound target 在报告中诚实计数但没有 repository CAS 可复制；orphan 只报告不删。
- 非 O_TMPFILE 文件系统的 named-temp claim fallback 若掉电只持久化 temp、尚未 link claim，可能需人工清理
  exact temp；目标 VEPFS 已实测使用 O_TMPFILE 快路。两节点 power-loss canary 留给 CP11.4c.3c.2。
- 无 DDL。代码回退：`git revert d72da353aefa8dc4f7b437ee08b2e197251fd7ff`。回退不会删除已发布的
  repository/dependency CAS 或 completion receipt；若回退时 target 仍带 `.restore-in-progress`，旧启动路径也会
  fail-closed 保留现场，须完成恢复或人工核验后再清理。
