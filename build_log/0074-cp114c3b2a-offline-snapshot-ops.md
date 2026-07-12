# 0074 · CP11.4c.3b.2a offline snapshot ops

- date: 2026-07-12
- commit: `011c98b0f4efdd2d3cb3152801126403fc01ca49` — feat: add CP11.4c.3b.2a offline snapshot ops
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3b.2a（属：步⑪ CP11.4c.3b 存储治理）

## 决策

在 b.1 同步快照主干上补一条薄的离线运维闭环，不新建 daemon、第二套 DB 或自动 GC：官方 CLI
必须取得 exact work-root 的既有 instance lease；SQLite 仍是唯一结构化真相。默认只深验/保护最近 3 代，
更老对象在 restore 或 GC 触及时再读全量，避免百轮历史每次重复做全部 SQLite 深验。

restore 明确只恢复 SQLite truth 到不存在的新 work-root，首启走 adoption；GC 必须先输出外部保存的
canonical plan，再由显式 plan hash 应用。applied-plan authority 先耐久，generation 随即逻辑退役，之后
才允许物理 unlink。registered checkpoint/log/import closure 留给 b.2b，不把本检查点冒充完整灾备。

## 改动

- 新增 `orchestrator/storage_ops.py`：lease-fenced `verify` / `restore` / `gc-plan` / `gc-apply` CLI；
  无 live DB 的 pointer/manifest/views Git 链验证；最近 3 代 SQLite 深验；同 fd copy+hash 的 no-clobber
  原子恢复；last-3 retention 与 backup CAS expired/orphan GC。
- GC 以 canonical plan + 显式 hash 绑定 high-water、protected window 和 exact victims；authority 文件及
  `sha256 → applied → gc → storage` 目录链在首次/resume 的任何 unlink 前重新 fsync。rename 前 kill temp
  可只读忽略并在 apply 下回收；rename 后、unlink 前 kill 会显示 `expired_but_present`，restore 已拒绝，
  重放只补物理删除。
- `CycleSnapshotPublisher` 增加只读布局模式、read-only Git `GIT_OPTIONAL_LOCKS=0` 和 backup 前 bytes/inodes
  headroom 门。development 用本次 backup + 小工程余量；production reserve 来自已验证 prerequisite
  envelope，`statvfs` 只代表当前物理余量。
- README/ROADMAP 固定命令、作用域与 b.2a/b.2b 边界；恢复目标经 resolved containment + Linux
  `renameat2(RENAME_NOREPLACE)`，临时清理只处理已知文件，不递归删除替代树。

## Review

- 内部参考/契约审查最终 `APPROVE`：确认 verify 不是 registered asset closure，restore 是
  `sqlite_truth_only`，b.2 整体仍未完成。
- 内部安全审查先后发现并修复：resolved target 绕回 source、authority 已落但仍可 restore、父目录 fsync、
  Git index dry-run 写、Python API 绕 lease、authority rename/unlink 掉电窗口和递归 temp 清理；两路最终均
  `APPROVE`。同 host root/orchestrator UID 主动替换治理路径仍按 README §7 的既有信任边界处理，不为此在
  b.2a 增加一套 hostile-host 文件能力层。
- 外审第 1 轮：`codexro` 凭证失效，HTTP 401，无 verdict。
- 外审最终第 2 轮：完整 staged diff 已内联并被 reviewer 接收，但 5 分钟内仅返回开始审查消息、无问题或
  verdict；按两轮上限终止，不再发第 3 轮。

## 验证

- 相关拆分批次共 **118 passed**：`test_storage_ops.py` 12、`test_storage_governance.py` 18、
  `test_run.py` 58、`test_advancer.py` 30。最后的 authority durability/temp cleanup/inode 调整后重跑
  storage ops **12 passed in 8.72s**、storage governance **18 passed in 9.69s**。
- `python -m py_compile`、`git diff --check`、staged diff check 均通过。
- 未跑全量：遵守用户“中间只跑相关验证、最终检查点才做一次全量”的要求；当前 20G overlay 仅约 78MB
  可用，也小于上一轮全量已知 191MB basetemp，强行启动只会制造 ENOSPC 假失败。

## 遗留 / 回退

- b.2b 仍须补 raw-log 确定性压缩镜像（保留冻结原件）与 import-materialization indexes/objects 的
  可达闭包核验/恢复；当前 GC 不删除 staging、checkpoint、content store、registered log 或 import object。
- backup 与活库仍在同一 VEPFS failure domain；SQLite-only restore 不是 work-root/fileset 或跨站灾备。
- 无 DDL。代码回退：`git revert 011c98b0f4efdd2d3cb3152801126403fc01ca49`。Git 回退不会复活已由操作员
  显式 apply GC 删除的 backup；该类数据恢复必须依赖另存副本。
