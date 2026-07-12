# 0071 · CP11.4c.2b.3b fixed GPU device bridge

- date: 2026-07-12
- commit: `563a496f50a57cbc0f1a8d250c1bc5c699fe323f` — feat: add CP11.4c.2b.3b fixed GPU device bridge
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.2b.3b（属：步⑪ CP11.4c.2b.3 deployment trust contract）

## 决策

以“固定部署分配”为最小可用边界，把部署证明中的 exact NVIDIA GPU UUID 从 production preflight 传到
Docker DeviceRequest、create inspect、受信 launcher 容器内 identity check 与 guardian-owned canary。不新增
GPU 调度器、MIG、动态租约或 DB 状态机。

- deployment attestation 升为 v2：GPU map 表达 service exact allocation，不再把 whole-host inventory 当授权。
- 启动预检分两相：恢复前纯读 static candidate，恢复后由 guardian GPU canary 完成最终证明；候选证明
  deep-freeze/内容寻址，canary authority 必须与 log、guardian receipt、owner/fence/engine/host/resource/spec 完整对上。
- plan/manifest 显式选择 `gpu_required`。GPU capability projection 纳入可复用 workload/runtime hash，exact UUID 仅留在
  invocation/runtime identity，避免在同能力的固定设备间无意义地打碎 cache。CPU/GPU 结果不可交叉复用。
- GPU 不可用、contract 缺失或不匹配时 fail-closed；已解析 target 以 `env_invalid` 终止，避免恢复后重启楔死。
- GPU+cgroup 保留 cgroup memory hard boundary，不再施加有限 RLIMIT_AS，避免 CUDA 虚拟地址预留陷阱；
  rlimit fallback 仍保持有限上限。

## 改动文件

- `meta-research/README.md` — 修改：补 fixed GPU 部署合同、两相 preflight、运行时身份与当前节点边界。
- `meta-research/orchestrator/attack_stages.py` — 修改：分离 raw sandbox runtime hash 与 plan-selected workload hash，依赖
  image 解析用前者，科学结果/复用身份用后者；GPU contract 缺失转 `env_invalid`。
- `meta-research/orchestrator/compiler_sqlite.py` — 修改：注入真实 sandbox runtime hash，只输出 plan 选定的 workload
  identity，并将运行时事实 source 标为 `runtime:execution-sandbox`。
- `meta-research/orchestrator/dependency_image_runtime.py` — 修改：派生 dependency sandbox 继承当前 exact GPU contract。
- `meta-research/orchestrator/deployment_preflight.py` — 修改：attestation/preflight protocol v2、static candidate 内容寻址、
  guardian canary 最终化及耐久 authority 交叉核验。
- `meta-research/orchestrator/execution_sandbox.py` — 修改：exact NVIDIA contract、Docker `--gpus` request/inspect、launcher
  inventory check、guardian canary/恢复、GPU-aware resource boundary 与 workload hash projection。
- `meta-research/orchestrator/manifest.py` — 修改：绑定 plan/bundle `gpu_required`、计算对应 workload hash，拒绝 CPU/GPU
  环境身份不一致。
- `meta-research/orchestrator/run.py` — 修改：装配 static preflight → recovery → guardian GPU canary → final gate；仅在
  GPU access、cgroup 与 Docker limits 均为真实 cgroup-v1/v2 时提升 GPU sandbox。
- `meta-research/prompts/skills/bundle/SKILL.md` — 修改：bundle 必须复制 plan 的 GPU 选择并匹配 workload hash。
- `meta-research/prompts/skills/plan/SKILL.md` — 修改：新 plan 必须显式给出 `gpu_required` 布尔值。
- `meta-research/schemas/deployment_attestation.schema.json` — 修改：冻结 v2 exact GPU allocation 字段与封闭结构。
- `meta-research/schemas/execution_manifest.schema.json` — 修改：新增 manifest `gpu_required` 契约。
- `meta-research/schemas/plan.schema.json` — 修改：新增 legacy-compatible、默认 false 的 `gpu_required`。
- `meta-research/tests/fixtures/invalid/deployment_attestation/missing_inode_quota.json` — 修改：迁移无效 attestation fixture 到 v2 GPU 结构。
- `meta-research/tests/fixtures/valid/deployment_attestation/basic.json` — 修改：迁移有效 attestation fixture 到 v2 exact allocation。
- `meta-research/tests/test_attack_advance.py` — 修改：覆盖 CPU/GPU workload 复用隔离与缺 contract 时 `env_invalid` 收敛。
- `meta-research/tests/test_compiler_sqlite.py` — 修改：覆盖 runtime source 和 plan-selected workload identity 编译。
- `meta-research/tests/test_dependency_image.py` — 修改：覆盖 dependency sandbox 继承 exact GPU contract。
- `meta-research/tests/test_deployment_preflight.py` — 修改：覆盖 v2 候选/最终 receipt、深冻结、canary authority 篡改、
  DB-less recovery 和 GPU count 正反例。
- `meta-research/tests/test_execution_sandbox.py` — 修改：覆盖 exact DeviceRequest/inspect/inventory、guardian canary、
  CPU/GPU hash、RLIMIT/cgroup 边界与恢复。
- `meta-research/tests/test_frozen_contracts.py` — 修改：更新 skill/schema 冻结契约断言。
- `meta-research/tests/test_manifest.py` — 修改：覆盖 plan/bundle GPU 选择、workload hash 交叉核验和 legacy CPU。
- `meta-research/tests/test_recall_sqlite.py` — 修改：覆盖 CPU/GPU 科学结果不可跨环境复用。
- `meta-research/tests/test_run.py` — 修改：覆盖两相启动顺序、cgroup/limits 提升矩阵、canary 异常与 final-false。
- `meta-research/tests/test_schemas.py` — 修改：覆盖 v2 attestation 及 plan/manifest `gpu_required` schema 正反例。

## Review（codex-chatgpt gpt-5.5/xhigh）

- 内部三路只读审查：修复了 CPU/GPU hash 交叉复用和缺 GPU contract 的 restart wedge；最终无剩余 BLOCKER。
- 第 1 轮：`codexro-review` 账号鉴权 401；同轮 inline fallback 读全量 diff 后试图调 shell，bwrap 失败，
  继续推理但未产出 output/verdict，按等待边界终止。
- 第 2 轮：明确禁止执行命令、内联全量内容后返回 `REQUEST_CHANGES`（2 BLOCKER + SHOULD/NIT）。
  - 采纳可维护性建议：构造唯一 `canary_context`，显式携带 `log_name` 并同时传入 prepare/run_staged；
    修正 compiler runtime source 标签。
  - 未采纳“recovery 重复 finalize 会移动 partial log”：`finalize_sandbox_output` 只处理 output quarantine，不移动 log；
    既有恢复用例与新增 promoted receipt 断言均通过。补了代码注释使边界更明确。
- 已达两轮上限，处置反馈后不发第 3 轮。

## 验证

- 相关测试多批执行：`python -m pytest -q` 覆盖 execution sandbox、deployment preflight、run 装配、
  manifest/schema、compiler/recall、attack/recovery 和 dependency image。阶段结果为 96、112、130、125（4 deselected）；
  最终相关集为 **427 passed, 3 deselected in 93.70s**，外审修复后定向为 **48 passed**。
- 边界检查：`git diff --check`、`python -m py_compile` 与 schema checks 通过。Docker 24.0.9 fake Unix API
  证明请求为 `Driver=nvidia`、`Count=0`、exact `DeviceIDs`、`Capabilities=[[compute,utility,gpu]]`、`Options={}`，
  create inspect 反核结构一致。
- 当前节点真实 negative GPU canary：`ok=false`，guardian return code 125，`container_drained=true`，
  无遗留 labeled container；正向 canary 因本机无 NVIDIA container runtime 未执行。
- 检查点末唯一全量命令：
  `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q --basetemp=/tmp/cp114c2b3b-full`

  ```text
  1 failed, 1494 passed, 1 skipped in 371.97s (0:06:11)
  FAILED meta-research/tests/test_dependency_image.py::test_exact_image_build_reuse_and_archive_restore
  write .../.docker_temp_1858849076: no space left on device
  ```

  唯一失败发生在既有 dependency image `docker save` archive 阶段；之前 build/install/runtime/pip 检查已成功，
  与本检查点 GPU diff 无关。该 Docker backing store ENOSPC 在前序检查点已有同类记录；依用户指示
  只跑一次全量，未二次重跑。
- 结论：功能相关契约与回归通过；全量不是全绿，唯一失败为当前节点容量环境问题，如实保留。

## 遗留 / 回退

- 当前节点可用于 CPU/development 链与 GPU 缺失时的 fail-closed 验证，不具备 NVIDIA container runtime，
  不能声称 production GPU 或 CP11.4c.3 已验收。
- 目标部署前需扩容 Docker backing store，准备 non-root service/private rootless cgroup daemon + NVIDIA
  runtime、GPFS quota attestation、隔离 Codex auth 和两节点 VEPFS。
- CP11.4c.3 仍需真实 100+ 轮 Codex/import/训练 soak 及 owner-kill、daemon-loss、预算/资源失败注入；
  CP11.3c 的 120 轮仅是控制面/状态稳定性回归。
- 回退：`git revert 563a496f50a57cbc0f1a8d250c1bc5c699fe323f`。无 DDL；回退后 deployment attestation 回到 v1，
  沙箱恢复 CPU-only 合同，既有 research DB 不变。
