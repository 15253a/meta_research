# 0069 · CP11.4c.2b.2c reviewed adapter generation

- date: 2026-07-11
- commit: `c0ba5ed8569e2fa8db0b5adb07f57f6f9b7d17ac` — feat: add CP11.4c.2b.2c reviewed adapter generation
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.2b.2c（收口 CP11.4c.2b.2 repository closure；CP11.4c.2b.3/3 继续）

## 决策

缺失显式 adapter 时只给模型一份由已验证 repository ledger/tree 导出的有界投影，执行一次 tool-free 生成和一次
独立 tool-free 评审；评审通过后仍由既有机械编译器、exact image sandbox 和 smoke 决定能否物化。没有增加多轮语义
状态机、第二套 receipt store 或隐式依赖安装。

- projection 对 inventory、预览文件数、单文件、总预览和最终 JSON 都有 byte/path 上界，优先浅层 README、配置、
  入口、artifact 与 source；普通 requirements/Poetry/uv lock 只列为不可用证据，不执行在线 pip。只有唯一 canonical
  `.meta-research/python-wheel-lock.json` 才能沿用 adapter v3 exact dependency image。
- 生成和评审复用现有 `runner_call`、`CostLedger`、heartbeat 与 `DECISION`，同 cycle+identity 可恢复复用；模型/配置、
  prompt、projection、candidate、adapter 和 verdict hash 都进入审计。provider/runtime/owner/budget/DB 故障保持基础设施
  故障，不永久归罪候选仓库；生成或评审的语义拒绝才成为 candidate materialization failure。
- 通过 sidecar 只嵌入 snapshot `spec.json` 的 `adapter_control`，不改冻结 Git tree/ledger。首次接收和 cache reuse 都会
  复核当前 projection/policy/candidate/adapter hash，防止旧 verdict 或篡改 sidecar 被复用。
- 科学 target identity 使用 origin、adapter bytes、projection hash 和 generation policy hash 的稳定执行身份，不混入
  DB-local runner/decision ID 或自由文本 review note；完整审计 provenance 仍保留在 supply-chain control hash。
- 显式 adapter 路径完全不调用生成器。ImportWorker 继续接收原 7 字段候选，只把精确
  `{cycle_id, external_import_id, question_id, candidate_id}` context 传给支持生成的 materializer；legacy fetch 路径不变。

## 改动文件

- `repository_adapter_projection.py` / `repository_adapter_generation.py` — 有界冻结投影、单次生成、单次独立评审、
  账本/decision 生命周期和稳定策略身份。
- `repository_materializer*.py` — 缺 adapter 分流、sidecar 机械编译、provenance/cache 复核和稳定 target identity。
- `import_worker.py` / `run.py` — 保持候选协议兼容的 import context bridge 与 tool-free production assembly。
- adapter generation/review schemas、SKILL、policy 与 README — 冻结输入输出、预算、重试及能力边界。
- repository generation/materializer/import/run/schema/skill tests — 覆盖恢复、成本、基础设施错误分类、篡改、identity、
  显式 adapter 兼容和装配。

## Review（两轮上限）

- 第 1 轮：隔离 `codexro-review` 账号返回 HTTP 401，refresh token 已 invalidated/revoked，无代码意见或 verdict。
- 第 2 轮：`codex-chatgpt` 以完整内联 staged diff、xhigh/read-only 启动，长时间无输出并进入 websocket reconnect；在
  等待上限人工终止，无 output file、代码意见或 verdict。已到两轮上限，不发第 3 轮。
- 内部并行只读审计发现并在功能提交前修复两项 blocker：当前 projection/policy/adapter 未被 provenance 强绑定，以及
  DB-local ID/free review text 污染科学 target identity。另修复基础设施 `ValueError` 被 ImportWorker 永久归罪候选、
  projection 把测试文件过度提权和默认预算偏大。最终内部审计无剩余代码 blocker。

## 验证

- 开发期按指示只跑相关验证：阶段性 **`325 passed in 70.39s`**；修复后 focused **`185 passed in 18.23s`**；
  projection/service/schema/SKILL **`115 passed in 2.36s`**；最终 service **`12 passed in 1.23s`**。materializer
  identity/provenance 定向、`py_compile` 和 `git diff --check` 均通过。
- 真实 missing-adapter canary 使用实际 tool-free Codex 生成/评审并准备进入 Docker smoke，但生成调用立即因隔离
  `/home/codexro/.codex` refresh token 失效返回 HTTP 401。系统按未知用量写 `cost_accounting_failed` 并 fail-closed，
  没有伪造成功或永久归罪候选。恢复该账号认证需要用户/运维授权，本检查点不能伪称实机通过。
- 检查点末只运行一次 VEPFS 临时目录全量：**`1443 passed, 1 skipped, 1 failed in 1051.15s`**。唯一失败为既有
  `test_dependency_image.py::test_exact_image_build_reuse_and_archive_restore` 在 `docker save` 时由 daemon 返回
  `write /ebs/docker/tmp/.../layer.tar: no space left on device`；与 adapter generation diff 无关。精确派生测试 image 已清理；
  环境仍缺 Docker tmp 空间，依“只做一次全量”指示未把同一全量重跑伪装成通过。
- 结论：代码层的缺 adapter 受审生成闭环和 CP11.4c.2b.2 repository closure 已完成；当前机器要实际启用该路径，
  仍须先恢复隔离 Codex 凭证并跑通一条真实 generation→review→Docker smoke canary。

## 遗留 / 回退

- 可用性边界：显式 adapter v2/v3 继续可用；自动生成路径在有效 Codex 凭证下才可用。普通 requirements/Poetry/uv
  项目不会被隐式安装，常见科学仓库覆盖率仍受 bootstrap image/canonical wheel lock 限制。
- CP11.4c.2b.3：以最小部署合同对 service account/VM、Docker socket、cgroup/device/GPU、VEPFS hard byte+inode
  quota 做启动前 fail-closed 预检；不另造编排系统，开发模式不得冒充生产通过。
- CP11.4c.3：目标 VEPFS 两节点和含真实 Codex/import/训练/故障注入的 100+ 轮 soak。
- 回退：`git revert c0ba5ed8569e2fa8db0b5adb07f57f6f9b7d17ac`。无 DDL；revert 后显式 adapter 路径保持，
  缺失 adapter 恢复为明确拒绝，既有只读 snapshot object 可留作审计或按运维策略回收。
