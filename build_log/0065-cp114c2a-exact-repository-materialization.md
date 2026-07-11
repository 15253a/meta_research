# 0065 · CP11.4c.2a exact repository snapshot 与 worker bridge

- date: 2026-07-11
- commit: `50ba41f4dadd45183bf201edda0f800c972dac00` — feat: close CP11.4c.2a exact repository materialization
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.2a（属：步⑪ CP11.4c.2 通用 pinned repository 物化与部署信任；CP11.4c.2b/3 继续）

## 决策

本检查点闭合“默认 GitHub discovery 的 pinned commit 如何成为 ImportWorker 可执行、可复核、可恢复的文件快照”，
不把缺 adapter、LFS/项目依赖安装或当前 host/Docker 信任域伪装成已经解决。

- production fetcher 对 40-hex GitHub commit 逐个读取 non-recursive Git tree object，重算 root/subtree Git SHA-1；
  commit archive 不调用 `extract()`，逐项核 path/type/count、size、executable mode 与 Git blob SHA，再生成 SHA-256 ledger。
- 固定 gitlink 子模块按 exact revision 递归物化，只接受受限 GitHub URL；根仓库与子模块的 license evidence path/content
  SHA 都必须和同一 commit 文件 ledger 对账，子模块另过 SPDX allowlist。symlink、Git LFS pointer、ConfigParser
  `DEFAULT` 歧义和未验证 dependency lock 均 fail-closed。
- verified tree/spec/transport/receipt 发布为 `<work>/state/import-materializations/{objects,indexes}` 下的只读内容寻址对象；
  gzip/tar 压缩字节只留 transport evidence，不进入复现身份。本地 index/object 损坏归 cache authority 错误，HTTP/network
  归 retryable transport；并发 object publication 只复用重新核验通过的胜者。
- adapter v2 只允许 pinned image 内直接 Python argv、`pinned_image_only` 与空项目 lock，显式声明 artifact、smoke/eval、
  非空 protocol scope、named metrics、compute/readout。protocol/metric family ID 稳定且不超过 JSON safe integer；同 family
  /version 的 scope、direction、unit、compute/readout 漂移由 worker 与 Gate 双层拒绝。
- ImportWorker 不再把整仓 bytes 塞进 DB；它消费 `source_tree + file_ledger + repository_snapshot_hash`，fd-safe 复制只读 clone，
  依次通过 adversarial sandbox smoke、独立 code review、延迟 protocol 注册、named factory eval、result review 与 pool gate。
  smoke/review 未过的仓库不会污染 append-only protocol registry；旧 embedded materialization v1 保持可恢复。
- 大仓库 judge 材料只给文件数/总 bytes、至多 256 条且 32KB 的 path inventory，并在 160KB 总预算内优先 adapter、命令入口、
  Python；其余 bytes 仍在 subject hash 闭包中，但明确不冒充已被语义审阅。不可信代码/log 中的提示注入不具有指令权。

## 改动文件

- `meta-research/orchestrator/repository_materializer.py` — GitHub transport/tree/archive/submodule/license、adapter v2、稳定身份、
  内容寻址 cache/index 发布与 production/legacy fetch 路由。
- `meta-research/orchestrator/import_worker.py`、`gate_pool.py` — file-backed clone/capability、延迟且可恢复的 factory protocol 注册、
  named metric 解析、import worker marker 的 protocol authority 与语义碰撞拒绝。
- `meta-research/orchestrator/artifact_capability.py`、`execution_sandbox.py` — 长文件 hash/copy 的 owner progress guard、sandbox 输入总量
  前置核、tree copy/fsync/cleanup，以及 pinned image compiler env 读取。
- `meta-research/orchestrator/run.py` — Docker preflight 后、SQLite/connector 前创建 repository materializer，并装入默认 worker。
- `meta-research/orchestrator/stage_provider.py`、`prompts/skills/judge/SKILL.md` — 大仓库有界/优先预览、非常规文件拒绝、材料不足与
  prompt injection 的诚实 judge 契约。
- `meta-research/policies/policy.yaml`、`schemas/policy.schema.json`、`schemas/import_adapter.schema.json` — archive/tree/file/submodule/
  compiler 上限与封闭 adapter contract。
- `meta-research/tests/test_repository_materializer.py`、`test_import_worker.py`、`test_stage_provider.py`、`test_run.py`、
  `test_schemas.py` 及 adapter fixtures — exact reuse、submodule/license、LFS/symlink、cache/transport、publish race、稳定语义、
  production candidate bridge、named metric、bounded judge 与默认装配回归。
- `meta-research/README.md`、`ROADMAP.md` — 使用面、诚实限制与 CP11.4c.2a/2b 切分。

## Review（codex-chatgpt，第二轮上限）

- 第 1 轮：`codexro-review` 因独立账号 token revoked/invalid 返回 401，未形成 verdict。
- 第 2 轮：fallback `codex-chatgpt` 对完整 staged diff 做内联只读审查，结论 `REQUEST_CHANGES`：
  - metric drift BLOCKER：Gate 原本已无条件比较 `metric_def(id,version)` 的 name/direction/unit/compute_spec，且 readout 已规范化
    进入 compute_spec，因此“静默复用”判断不成立；仍采纳纵深防御，在 worker 调 Gate 前独立比较，并新增“protocol 升版但
    metric 不升版且改 readout”负例。
  - candidate 字段 BLOCKER：代码已从 `external_candidate` 查询并组装 `source_kind/search_snapshot_json/search_snapshot_hash`，是大
    diff 漏读；新增 `ImportWorker → ProductionCandidateFetcher` 完整封闭字段回归证明生产 bridge。
  - `attack=False` SHOULD：`ProductionCandidateFetcher`/ImportWorker 只在 `if attack is True` 分支构造，误报；另在 fetcher 构造器
    增加 callable fail-fast，防未来错误装配退化成 `NoneType` 调用。
  - 并发 publication SHOULD（成立）：`EEXIST/ENOTEMPTY` 改为验证已发布胜者、清理本次 staging 后复用，并加模拟竞态回归。
  - worker 允许空 compute/readout SHOULD（成立）：worker 自身也改为要求非空字符串，不能只依赖上游 materializer/schema。
  - 1800+ 行模块 NIT：本检查点未在外审后做高风险机械拆分；transport/tree/archive/adapter/cache 的方法边界已封闭，后续
    CP11.4c.2b 扩展 LFS/dependency/adapter generator 前应先按这些边界拆模块，并保留本检查点回归作为护栏。
- 已到两轮上限，不发第 3 轮；成立问题均在功能提交前修复，误报均给出代码证据/回归。

## 验证

- 核心契约相关集：
  `pytest -q meta-research/tests/test_repository_materializer.py meta-research/tests/test_import_worker.py meta-research/tests/test_stage_provider.py meta-research/tests/test_schemas.py meta-research/tests/test_skills.py`
  → **`170 passed in 78.86s`**。
- 集成相关集：
  `pytest -q meta-research/tests/test_artifact_capability.py meta-research/tests/test_execution_sandbox.py meta-research/tests/test_gate_pool.py meta-research/tests/test_run.py meta-research/tests/test_manifest.py`
  → **`156 passed in 109.93s`**。
- 第 2 轮外审修复相关集：
  `pytest -q meta-research/tests/test_repository_materializer.py meta-research/tests/test_import_worker.py`
  → **`41 passed in 48.65s`**；`py_compile` 与 `git diff --check` 通过。
- 按用户要求，开发期不跑反复全量；功能冻结后只运行一次最终全量，并把临时空间固定到 VEPFS：
  `TMPDIR=<vepfs>/tmp pytest -q --basetemp=<vepfs>/final-cp114c2a`
  → **`1381 passed in 1024.62s (0:17:04)`**，无失败。
- CP11.4c.2a 结论：带受支持 adapter 的 pinned GitHub commit 已能以 exact file-backed snapshot 进入 sandboxed import 全链；
  该结论不包含缺 adapter 自动适配、LFS/项目依赖安装、部署信任合同或真实 100+ 轮运营验收。

## 遗留 / 回退

- CP11.4c.2b：先拆分 materializer 内部 transport/tree/archive/adapter/cache 组件，再实现缺 adapter 的受审生成、Git LFS
  batch OID+size 核验、项目 lock 构建/验证与专用 image；把 service account/VM、Docker socket、cgroup/device/GPU、
  VEPFS hard byte+inode quota 做成启动 fail-closed contract，节点不具备的能力不得伪造通过。
- CP11.4c.3：目标 VEPFS 两节点 owner/lease/fd canary，以及含真实 Codex/import/训练、owner-kill/daemon-loss/预算/资源失败的
  100+ 轮 soak 与可重放证据包。CP11.3c 的控制面 120 轮不能替代。
- 回退前停止 orchestrator，确认没有 import target/sandbox invocation 在途，并保留已发布 object/index/receipt 供审计；执行
  `git revert 50ba41f4dadd45183bf201edda0f800c972dac00`。本提交无 DDL migration，但旧代码不理解 repository adapter v2 与
  file-backed plan_ref，不能在新 import worker 在途时热回退。
