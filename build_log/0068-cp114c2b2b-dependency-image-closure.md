# 0068 · CP11.4c.2b.2b dependency image closure

- date: 2026-07-11
- commit: `1e5761156a6075c48b9822f7e4ffe00e6e2a488b` — feat: close CP11.4c.2b.2b exact dependency images
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.2b.2b（属：步⑪ CP11.4c.2b.2 repository closure；2b.2c/2b.3/3 继续）

## 决策

把 repository dependency 从“bootstrap image 自带或拒绝”提升为一个可保存、可恢复、可继承的 exact Python wheel
image capability，但不执行仓库 Dockerfile/setup script，不允许 sdist、host install、动态 tag/pull 或无 hash dependency。

- adapter v2 继续只接受 `pinned_image_only` 且 lock 为空；新增 v3 `python_wheel_image_v1`，只接受一份
  `.meta-research/python-wheel-lock.json`。lock 必须 canonical JSON+LF，绑定 exact CPython artifact/platform，每个 wheel
  必须有 HTTPS URL、SHA-256 和 bytes，并按 package/version/filename 排序。
- 公网只访问 policy allowlist 的 `files.pythonhosted.org`，拒绝 userinfo/query/fragment/control character、非默认端口、
  cross-origin redirect 和 SSRF host。wheel 只允许与 CPython 3.11/Linux amd64 兼容的保守 tag 子集，核验 archive
  path/type/encryption、top-level dist-info METADATA/WHEEL/RECORD、Name/Version，并对整个 closure 累计压缩 bytes、解压
  bytes、entry、安装 files/bytes。
- wheel 由 pinned bootstrap `DockerExecutionSandbox` 在 `network=none`、uid 65534、seccomp/rlimit 下用 pip
  `--no-index --no-deps --only-binary=:all:` 安装到隔离 target。生成 Dockerfile 只有 `FROM <exact local image ID>`、
  `COPY site-packages`、closure `LABEL`；context file/dir bytes、mode、mtime 都归一并再核。
- legacy Docker 的 build timestamp/intermediate image 不保证二次 build 同 ID，因此不伪称 rebuild bit-identical。首个验收
  result image 以 `docker save` 导出，用 RLIMIT_FSIZE 加 policy 上限，记录 exact archive SHA-256/bytes、engine identity、
  base/result image ID、RootFS ancestry、全 Config 继承和 runtime/compiler/pip-check receipt；本地丢 image 后必须从 archive
  恢复同一 image ID。
- 内容寻址 object 发布前 fsync 全部文件，以 0400/0500（context 为 0444/0555）冻结；复用时重算 lock、
  receipt、context、runtime evidence 和 archive。owner loss/stale staging 先排空 guardian/container，再按 closure label 精确删除
  未发布 image，避免长期运行堆积 Docker 垃圾。同一 resolve 只做一次大 archive 核验。
- derived sandbox 把 `PYTHONPATH=/opt/meta-research/site-packages` 作为可信、不可被 task env 覆盖的 identity-bound
  payload environment。ImportWorker 只从可信 capability resolver 取 image；compiler/AttackStages 从 baseline 的成功
  `external_import` checkpoint 机械继承 `env_hash`，后续 smoke/train/eval 不能由 Codex 自选 image。

## 改动文件

- `meta-research/orchestrator/dependency_image*.py` — lock/download/archive 核验、sandbox install、generated context/image/archive、
  immutable object store、engine/recovery 与 capability resolver；按 common/lock/runtime/store/build 职责拆分。
- `repository_materializer_adapter.py` / `repository_materializer_store.py` / facade/common — adapter v2/v3 分流、dependency
  capability 与 supply-chain/target identity 绑定、cache reuse 再解析。
- `execution_sandbox.py` / `harness.py` — raw exact image ID 与 trusted `payload_environment`；host Docker control 显式不继承
  orchestrator environment/credentials。
- `import_worker.py` / `compiler_sqlite.py` / `attack_stages.py` / `run.py` — 组装 builder/resolver，把 import runtime
  identity 传到当前 import 和未来 bundle/smoke/train/eval。
- `policy.yaml`、`policy.schema.json`、`import_adapter.schema.json`、`python_wheel_lock.schema.json` — 冻结 provider、预算、
  URL/site-packages/adapter 合同，并把 payload environment 纳入 sandbox identity。
- `test_dependency_image.py` 及 repository/import/sandbox/compiler/attack/schema 回归 — 覆盖 SSRF/tag/lock 拒绝、真实
  Docker build/import、pre-publish failure 清理、cache tamper、两次 exact archive restore、resolver 唯一绑定与后续 env 继承。

## Review（codex-chatgpt，两轮上限）

- 第 1 轮：`codexro-review` 独立账号 token invalidated/revoked，HTTP 401，未形成代码意见或 verdict。
- 第 2 轮：fallback `codex-chatgpt` 已完整内联 staged diff 并成功启动 xhigh/read-only 审查，但服务端
  websocket 连接在 29 分钟内用尽 `Reconnecting 2/5` 至 `5/5`，无 output file、无代码意见/verdict，人工终止
  失去进展的进程。
- 已到两轮上限，不发第 3 轮。本地继续逐路径审计，发现并修复了 context metadata 未入 identity、失败后
  uncommitted image 泄漏、URL 字面规范化、wheel tag/累计解压边界、runtime evidence 复核和同一 resolve 重复大
  archive hash 等问题；所有修复均在功能提交前完成并经定向/全量验证。

## 验证

- 最终相关集：dependency image、repository materializer/boundary、ImportWorker、sandbox、schema、compiler、
  AttackStages、run → **`346 passed, 1 skipped in 322.57s`**。后续只对去重核验/restore 改动复跑真 Docker
  定向，最终 **`1 passed in 19.08s`**。
- 真实只读公网 canary：`idna-3.15-py3-none-any.whl`，URL/`72340` bytes/
  `sha256:048adeaf8c2d788c40fee287673ccaa74c24ffd8dcf09ffa555a2fbb59f10ac8` 精确绑定，下载、严格 wheel
  验证、offline install、image build/receipt/resolve → **`1 passed in 11.23s`**。
- `py_compile`/`compileall`、schema/policy tests、`git diff --check` 与 `git diff --staged --check` 通过。
- 按用户要求，开发期只跑相关验证；检查点冻结后只运行一次 VEPFS 临时目录全量：
  `pytest -q meta-research/tests --basetemp=<vepfs>/...` → **`1418 passed, 1 skipped in 1036.44s (0:17:16)`**。唯一跳过
  是默认关闭、但已单独开启通过的公网 canary。
- 结论：对显式 adapter v3 + canonical Python wheel lock，dependency bytes 到 exact/restorable project image、当前
  import 和后续实验 runtime identity 的闭包已可用；这不等于任意仓库自动适配或生产部署/百轮验收已完成。

## 遗留 / 回退

- CP11.4c.2b.2c：缺 adapter 时的有界 sidecar 生成、独立评审、sandbox smoke 与可恢复调用/产物审计。
- CP11.4c.2b.3：service account/VM、Docker socket、cgroup/device/GPU 和 VEPFS hard byte+inode quota 启动合同。当前
  `max_cached_images` 只给出数量上限，不能替代部署层硬 quota。
- CP11.4c.3：目标 VEPFS 两节点 exact archive/owner/lease/fd 实机行为，以及含真实 Codex/import/训练/故障注入的
  100+ 轮 soak 证据包。
- 回退：先确认没有 dependency image build/restore 在途，再执行
  `git revert 1e5761156a6075c48b9822f7e4ffe00e6e2a488b`。本提交无 DDL；revert 后 adapter v3 将被拒绝，已发布只读
  object/archive 可保留审计或按运维策略回收，Docker 中 derived image 需按 receipt/closure label 另行清理。
