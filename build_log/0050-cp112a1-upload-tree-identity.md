# 0050 · CP11.2a.1 嵌套上传树身份与遍历预算

- date: 2026-07-10
- commit: 3c4c9b460a2f90ea3e397837e4e5f2cbe7c5ede4 — fix: 固定嵌套上传树身份
- branch: fix/architecture-hardening-20260709
- 检查点 / 步: CP11.2a.1（属：步⑪ 生产硬化 · CP11.2 人类控制闭环的资产安全跟进）

## 决策

把 CP11.2a 对上传根的固定扩展到整棵上传树，并把它从尚未完成的 CP11.2b 控制面改动中拆出，保证两者可以独立回退：

- 从绝对根或调用方传入的 `/proc/self/fd/<n>` 目录 capability 起步，逐组件使用
  `openat(O_NOFOLLOW)` 打开上传根、条目目录、嵌套目录和 regular file；
- 枚举时记录目录身份与文件指纹，复制前、复制后均对路径项和已打开 fd 复验，拒绝目录/文件替换以及同 inode 原位改写；
- 文件枚举、复制、SHA-256 与文本预览始终使用同一个已验证文件 fd，不在验证后按路径重开；
- 拒绝 hardlink、symlink、FIFO/device 等非独占 regular file；
- 对单次 resolve 统一设置目录深度、单目录项数、总目录项数和总目录数预算，空目录和特殊项也计入预算，避免以非文件节点绕过资源上限；
- 保持相对路径排序确定性，并继续执行既有 goal-wide 文件数上限。

原始路径在任何 `abspath` 词法折叠前先拒绝 `..` 组件，避免 `symlink/..` 把未核验组件隐藏掉；目录组件数组与身份数组也显式要求等长，不能依赖 `zip` 的静默截断。

## 改动文件

- `ROADMAP.md` — 新增独立 CP11.2a.1 检查点，使上传树修复与 CP11.2b 控制面可分别回退。
- `meta-research/orchestrator/notify.py` — 实现逐组件固定、目录/文件身份复验、同 fd 消费和 resolve 级遍历预算。
- `meta-research/tests/test_notify.py` — 覆盖父组件 symlink、`symlink/..`、枚举后目录/文件替换、同 inode 原位改写、hardlink、三类遍历预算、pinned fd capability 与确定性顺序。

## Review

- 首次启动外审时 reviewer 凭证未就绪，没有产生 verdict，因此不计入两轮上限；按仓库约束恢复只读 reviewer 凭证后重新开始。
- 外审第 1 轮：`APPROVE`，同时给出两个 SHOULD 与一个 NIT：原始路径的 `..` 应在 `abspath` 前拒绝；应补同 inode 原位变更的复制后复验测试；目录身份配对应拒绝长度不一致。三项均核实并修复。
- 外审第 2 轮（最后一轮）：`APPROVE`，无 BLOCKER、SHOULD 或 NIT；reviewer 额外核对了 staged-only index、调用方 pinned directory fd 契约、`/proc/self/fd` 语义、遍历资源界限和语法。
- 外审证据：`/tmp/codexrev.g45W7a/verdict.md`、`/tmp/codexrev.hnAMsA/verdict.md`。

## 验证

- Git index 隔离快照定向：`pytest -q meta-research/tests/test_notify.py` → `80 passed in 5.72s`。
- Git index 隔离快照全量：`pytest -q meta-research/tests` → `857 passed in 157.12s`。
- 暂存版 `notify.py` / `test_notify.py` 的 `ast.parse` 通过。
- `git diff --cached --check` 通过。
- 外审第 2 轮 `APPROVE`。
- 结论：**通过**。

## 遗留 / 回退

- CP11.2b 仍在工作区，负责 HTTP console → durable spool → run 单写 ingest → 权威 confirm/reject/resolve/cancel，以及生产 notifier/前端闭环；本提交没有宣称控制闭环已完成。
- CP11.3/CP11.4 的单实例、进程组终止、状态语义、强执行隔离与内容寻址存储仍未完成。
- 代码回退：`git revert 3c4c9b4`。本提交无 DB migration；回退会恢复旧的嵌套遍历实现，不应在生产环境长期停留于该版本。
