# 0066 · CP11.4c.2b.1 repository materializer component boundaries

- date: 2026-07-11
- commit: `4e2869822123b002ced3443dfe4cc1f898e14a11` — refactor: split CP11.4c.2b.1 repository materializer boundaries
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.2b.1（属：步⑪ CP11.4c.2b repository closure/deployment trust；CP11.4c.2b.2–3/3 继续）

## 决策

先把 CP11.4c.2a 的 1800+ 行仓库物化单体按供应链职责拆开，再扩展 LFS、adapter 生成与依赖构建；本检查点只做
行为保持型重构，不改变 snapshot protocol/hash/cache layout、错误分类、发布路径或调用 facade。

- `repository_materializer.py` 保留配置/搜索快照校验、单次物化编排、production/legacy fetch 路由，以及原模块
  public/private compatibility symbols。
- canonical identity、严格 JSON/path/hash/LFS pointer 等有界 primitive 归入 common；GitHub HTTPS/redirect、Git object
  tree、archive/submodule/license、adapter compiler、content-addressed store 分别由独立组件拥有。
- facade 通过内部 mixin 组合原方法；拆分前后的 24 个 materializer 方法、15 个 helper、14 个模块常量与 6 个不拆类均做
  AST 等价核验，原模块 36 个可见符号也逐项检查无缺失。
- 新增边界回归，确保职责方法仍由对应组件实现而不是重新长回 facade；按外审意见不固定 mixin 精确顺序，允许未来兼容重排。

## 改动文件

- `meta-research/orchestrator/repository_materializer.py` — 缩为兼容 facade、配置/快照校验与物化编排入口。
- `meta-research/orchestrator/repository_materialization_common.py` — 新增共享协议常量、错误类型、canonical/hash/path/LFS primitive。
- `meta-research/orchestrator/repository_materializer_transport.py` — 新增有界 GitHub API/archive HTTPS 与 redirect policy。
- `meta-research/orchestrator/repository_materializer_tree.py` — 新增 exact commit/tree traversal 与 Git tree SHA 重算。
- `meta-research/orchestrator/repository_materializer_archive.py` — 新增安全 archive extraction、license、submodule 与递归 source snapshot。
- `meta-research/orchestrator/repository_materializer_adapter.py` — 新增 adapter v2 校验、稳定 protocol/metric 与 supply-chain 编译。
- `meta-research/orchestrator/repository_materializer_store.py` — 新增 authority directory、published object/index 完整性核验与复用。
- `meta-research/tests/test_repository_materializer_boundaries.py` — 新增组件归属/facade 不重复实现的架构回归。
- `meta-research/README.md`、`ROADMAP.md` — 记录扩展落点与 CP11.4c.2b.1–3 检查点切分。

## Review（codex-chatgpt，第二轮上限）

- 第 1 轮：`codexro-review` 的独立账号 token invalidated/revoked，HTTP 401，未形成代码意见或 verdict。
- 第 2 轮：fallback `codex-chatgpt` 对完整 staged diff 做全内联只读审查，结论 `APPROVE`，无 BLOCKER；确认未发现
  snapshot/hash/cache、错误分类、facade 或回放身份漂移。
- SHOULD（采纳）：原边界测试精确固定 `__bases__` 顺序会阻止无行为变化的重排；改为所需组件属于 MRO、方法由对应组件
  拥有且 facade 不重复实现，定向 `1 passed`。两轮上限后未发第 3 轮。
- NIT（不采纳）：组件类保持下划线私有，因为它们是 facade 内部实现边界而非新 public API；测试可直接守护内部架构合同。

## 验证

- 核心物化与边界：
  `pytest -q tests/test_repository_materializer.py tests/test_repository_materializer_boundaries.py`
  → **`16 passed in 0.99s`**。
- production import 装配相关：
  `pytest -q tests/test_repository_materializer.py tests/test_repository_materializer_boundaries.py tests/test_import_worker.py tests/test_run.py::test_default_attack_assembly_includes_fenced_import_worker`
  → **`43 passed in 50.41s`**。
- 机械等价探针：旧 HEAD 与拆分工作区 AST 比较 → **`methods=24 helpers=15`**、
  **`constants=14 exact_classes=6`**；原 facade 可见符号 → **`36, missing=[]`**。
- `python -m compileall -q`（7 个物化模块）、`git diff --check`、`git diff --staged --check` 均通过。
- 按用户要求，开发期只跑相关验证；功能冻结/外审结束后只运行一次 VEPFS 临时目录全量：
  `TMPDIR=<vepfs>/tmp pytest -q --basetemp=<vepfs>/full meta-research/tests`
  → **`1382 passed in 1038.17s (0:17:18)`**，无失败。
- 结论：本检查点的职责拆分和兼容性成立；它不声称已经实现 LFS、adapter 生成、依赖专用镜像、生产部署合同或 100+ 轮实机运营验收。

## 遗留 / 回退

- CP11.4c.2b.2：实现缺 adapter 的有界生成/独立评审、Git LFS batch OID+size 下载核验、dependency lock 到专用 exact
  image 的可复现构建与启动验收。
- CP11.4c.2b.3：把 service account/VM、Docker socket、cgroup/device/GPU、VEPFS hard byte+inode quota 变成启动前
  fail-closed 部署合同；当前 rootless/cgroup fallback 节点只能作为 development，不得报告 production-ready。
- CP11.4c.3：目标 VEPFS 双节点与含真实 Codex/import/训练、owner-kill/daemon-loss/资源失败的 100+ 轮 soak。
- 回退：`git revert 4e2869822123b002ced3443dfe4cc1f898e14a11`；本提交不改 DDL、协议或持久化格式，可在无在途物化任务时直接回退。
