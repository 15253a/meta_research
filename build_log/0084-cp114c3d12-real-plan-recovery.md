# 0084 · CP11.4c.3d.1.2 real plan recovery

- date: 2026-07-13
- commit: `ad6a303b0f6a90423cb349ec8d6bd69e2c8ef89a` — `fix(plan): lock synthetic protocols without false file requests`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3d.1.2（属：步⑪ CP11.4c.3d 基本可用 / 目标生产运行）

## 问题与决策

真实 full-attack smoke 从 c3 进入 plan 后，模型连续把自建 synthetic protocol 的 seeds、样本量、分布与
标签规则当成用户必须补交的既有资料，并三次输出 schema 不合法的 `resource_request.json`。这不是资源真的
缺失，而是 prompt 只给 schema 路径、未把“既有事实不得臆造”和“本 plan 的前瞻设计决定”分开。

采用最小修复：plan 必须自行选择并锁定 synthetic protocol 参数；不得把该职责转成文件请求，也不得把设计值
伪称为历史事实。system prompt 直接内联真实 schema 的最小合法 resource-request 骨架。StageProvider 在调用
interaction bridge 前聚合 schema errors 并作为一次 `artifact_parse` 反馈，保留无 bridge fail-loud、业务拒绝
有界重试和非业务异常原样失败。没有新增 schema、数据库、daemon、工作流或配置层。

同时补齐既有 plan schema 已要求、但 skill 漏写的 `eval/create_evaluation.claim`：必须给
`baseline_ref` 与 `variant_key`。

## 改动文件

- `meta-research/prompts/system_prompt.md` — 区分事实与前瞻设计，内联 exact request skeleton。
- `meta-research/prompts/skills/plan/SKILL.md` — 锁 synthetic protocol；补齐 evaluation claim 契约。
- `meta-research/orchestrator/stage_provider.py` — bridge 前聚合 sidecar schema errors。
- `meta-research/tests/test_skills.py`、`tests/test_stage_provider.py` — 锁 prompt/schema 与 pre-bridge 行为。

## 验证与 review

- TDD 精确 3 项先失败后通过；`python -m py_compile` 与 `git diff --check` 通过。
- `tests/test_skills.py tests/test_stage_provider.py tests/test_schemas.py`：`153 passed in 44.41s`。
- 真实恢复：同一 `.production-canary/full-attack-real` c3 checkpoint 上，新的 plan 首次生成成功，第二轮
  plan review PASS；bundle 生成、Docker build/run、code review、result review 均成功，run target complete；
  `storage verify` 通过 c1-c3 全部 snapshot。
- 内部只读审查 APPROVE：重放三份坏 sidecar 分别一次聚合 3/2/7 条错误，确认合法、quota 拒绝和异常边界未变。
- 外部只读审查 APPROVE，未发现 BLOCKER/SHOULD/NIT；外审环境的系统 Python 无 pytest，因此其结论使用
  staged diff、schema/reference 静态核对，测试数字来自主执行环境。
- 依用户要求，本中间检查点未跑仓库全量；全量只留最终验收提交前执行一次。

## 新暴露边界与回退

- bundle 产出四项真实 metric_result（aggregate accuracy、minimum per-seed accuracy、std、below-threshold rate），
  但 compiler 将锚展示为 `mr1:1@1=...`，reasoning 把该整串误作 `metric_result_id`；Gate 只接受 exact
  `mr1`，所以结论未落库。该问题单列 CP11.4c.3d.1.3，不在本提交中混修。
- reasoning skill 仍含 M0 阶段的 “fake execution” 文字，导致它错误描述本次真实 Docker run；同在 d.1.3
  以最小 prompt 修复处理。
- 当前 smoke 证明单节点真实 plan/bundle/CPU Docker 路径基本可用；不证明 GPU、生产 connector、双节点、
  ≥200 轮、T1/T2 或最终全量。
- 回退：`git revert ad6a303b0f6a90423cb349ec8d6bd69e2c8ef89a`。
