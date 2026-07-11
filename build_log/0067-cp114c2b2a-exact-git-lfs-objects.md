# 0067 · CP11.4c.2b.2a exact Git LFS objects

- date: 2026-07-11
- commit: `ef30f1ff5512c1d4dcd541f617da2682eac3cc17` — feat: close CP11.4c.2b.2a exact Git LFS objects
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.2b.2a（属：步⑪ CP11.4c.2b.2 repository closure；2b/2c/2b.3/3 继续）

## 决策

把 Git LFS 从“见 pointer 即拒绝”提升为可生产复核的 exact object closure，但不执行 `git-lfs`、不读取仓库提供的
`.lfsconfig`、不信任临时下载授权，也不把 GitHub/网络故障永久归罪候选。协议口径固定为 Git LFS 官方
[pointer spec](https://github.com/git-lfs/git-lfs/blob/main/docs/spec.md) 与
[Batch API](https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md)。

- 从已通过 Git tree/archive 双核的 `<1024` byte blob 识别 v1 pointer；malformed LFS-like 内容 fail-closed。普通 archive
  含 pointer 时，按所属 `repository` 派生固定 `https://github.com/<owner>/<repo>.git/info/lfs/objects/batch`，只发
  `download/basic/sha256` 的有界 canonical request；不接受仓库自定义 endpoint/transfer。
- Batch response 只接受 vendor JSON 或 GitHub 实测 `application/json`、basic/sha256、requested OID+size 精确闭包；action
  只接受 HTTPS、policy host allowlist 和有界 header。跨 host redirect 若携任意 action header 直接拒绝，避免临时凭据泄漏。
- object 先写私有临时文件，流式重算 pointer SHA-256 OID 与 size，fsync 后原子发布到本次 staging cache；同 OID bytes 可复用，
  但 root/submodule/不同 repository 仍逐个经过各自 Batch endpoint，不能借另一个 repo 的可用性冒充所属 repo 闭包。
- GitHub archive 若配置为直接包含 LFS object，先从 tree 指向的 Git Blob API 取回原 pointer，重算 pointer Git blob SHA，
  再用 pointer OID/size 双核 archive bytes；普通 archive 篡改仍按 Git blob SHA 拒绝。
- 最终 ledger 保存 actual `sha256/bytes`、pointer `sha256/bytes` 与 pointer Git blob SHA；spec 的 `lfs_objects` 绑定 path、
  repository、revision、OID/size。signed action URL/header 永不落盘，transport 只留 Batch request/response hash、脱敏 origin 与 bytes。
- 错误语义：pointer 格式/预算/per-object 404/410 为 durable candidate failure；Batch endpoint/response drift、Git blob response drift、
  host/header 围栏、下载/hash/IO 故障均为 retryable `RepositoryTransportError`，ImportWorker 不会错误 settle candidate。

## 改动文件

- `meta-research/orchestrator/repository_materializer_lfs.py` — 新增 Git Blob pointer 复核、Batch basic transport、action/redirect
  围栏、原子 OID 下载、per-repository availability 与 LFS ledger materialization。
- `meta-research/orchestrator/repository_materializer.py` — 装配 LFS component/opener/injection，校验 LFS policy/batch/object host 上限；
  production schema 固定 fetch，runtime 保留 reject 作为旧 policy/测试的更严格兼容模式。
- `meta-research/orchestrator/repository_materializer_archive.py` — 支持 pointer archive 与 GitHub 已展开 LFS archive 双路径，递归
  submodule 共享 unique-object 预算，并拒绝 LFS `.gitmodules`/license 控制证据。
- `meta-research/orchestrator/repository_materializer_adapter.py` — 把 path/repository/revision/pointer/OID identity 写入 supply chain。
- `meta-research/orchestrator/repository_materializer_store.py` — cache reuse 时验证可选 LFS ledger 字段与 actual tree hash/size。
- `meta-research/orchestrator/repository_materialization_common.py` — 按官方约束把 pointer 总长边界收紧为 `<1024` bytes。
- `meta-research/policies/policy.yaml`、`schemas/policy.schema.json` — production `lfs_policy=fetch`、10k unique objects、100/batch 与
  exact download host allowlist。
- `meta-research/tests/test_repository_materializer.py`、`test_repository_materializer_boundaries.py` — Batch/GET 真 HTTP 形状、Basic token、
  signed transport 漂移、archive-expanded、root/submodule、duplicate/cross-repo OID、redirect/header/SSRF、错误分类、预算与 cache 回归。
- `meta-research/README.md`、`ROADMAP.md` — 运维策略、官方协议来源、诚实限制与 2a/2b/2c 检查点切分。

## Review（codex-chatgpt，第二轮上限）

- 第 1 轮：`codexro-review` 独立账号 token invalidated/revoked，HTTP 401，未形成代码意见或 verdict。
- 第 2 轮：fallback `codex-chatgpt` 对完整 staged diff 做全内联只读审查，结论 `REQUEST_CHANGES`：
  - BLOCKER（成立）：Batch endpoint 404/410 原被当 candidate failure；改为 transport。per-object 只保留 404/410 durable，422
    与其它协议/服务错误均 retryable，并加 endpoint 404/response code 回归。
  - BLOCKER（成立）：archive-expanded 路径的 Git Blob API response/base64/identity drift 原会 settle candidate；全部改为 transport，
    并加 blob encoding drift 回归。
  - SHOULD（采纳）：虽失败会清整个 staging，仍把对象改为 temp→OID/size verify→atomic rename，消除局部损坏窗口。
  - SHOULD（采纳）：同 OID 跨 repository 仍分别 Batch 验证，只复用 bytes；新增 root+submodule same-OID 回归。
  - SHOULD（采纳）：跨 host redirect 不再转发任何 action-supplied header；有 header 时直接 transport failure。
  - NIT（澄清）：schema 固定 production fetch，runtime reject 是旧冻结 policy/定向测试的 fail-closed 兼容，不是第二套生产模式。
- 已到两轮上限，不发第 3 轮；两项 BLOCKER 与全部 SHOULD 在功能提交前修复，相关集重新通过。

## 验证

- 冻结相关集：
  `pytest -q tests/test_repository_materializer.py tests/test_repository_materializer_boundaries.py tests/test_import_worker.py tests/test_schemas.py tests/test_run.py::test_default_attack_assembly_includes_fenced_import_worker`
  → 外审修复后 **`149 passed in 53.81s`**；其中 materializer/LFS/boundary **`35 passed in 2.74s`**。
- `python -m compileall -q`/`py_compile`、`python -m json.tool schemas/policy.schema.json`、`git diff --check` 与
  `git diff --staged --check` 均通过。
- 真实只读 canary：公开 `Schoonology/git-lfs-test@951508f56a5275f8a5d82fc34150d1cb09aad835` 的 pointer
  `sha256:6fe2e48e…` / `620773` bytes。首次探针安全拒绝 GitHub 实际 `application/json`（规范示例为 vendor JSON），据真实响应
  收紧为两值集合并加回归；复跑经 `github-cloud.githubusercontent.com` 下载，结果 **`oid_match=True`、size=620773**。
  未登记候选、未改远端、未输出/持久化 signed URL/header。
- 按用户要求，开发期只跑相关验证；功能冻结与两轮外审反馈处置后只运行一次 VEPFS 临时目录全量：
  `TMPDIR=<vepfs>/tmp pytest -q --basetemp=<vepfs>/full meta-research/tests`
  → **`1401 passed in 1022.50s (0:17:02)`**，无失败。
- 结论：GitHub exact repository snapshot 的 LFS object closure 可用；这不包含 dependency image、缺 adapter 生成、部署信任合同
  或真实 100+ 轮运营验收。

## 遗留 / 回退

- CP11.4c.2b.2b：受支持 dependency lock 到 exact project image 的离线/有界 build context、base/result image ID、compiler/runtime
  smoke receipt；禁止 host install、隐式 pull/tag 与无 hash dependency。
- CP11.4c.2b.2c：缺 adapter 的有界生成、独立评审、sandbox smoke 与可恢复调用/产物审计。
- CP11.4c.2b.3/3：生产部署合同、双节点 VEPFS 与真实 100+ 轮故障注入 soak 仍未完成。
- 回退：先确认没有 LFS import worker 在途，再执行 `git revert ef30f1ff5512c1d4dcd541f617da2682eac3cc17`。本提交无 DDL；
  revert 后旧 reject config/hash 不会复用 fetch config 的 index，新代码发布的只读 object 可保留审计或按运维策略回收。
