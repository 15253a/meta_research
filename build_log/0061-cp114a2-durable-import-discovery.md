# 0061 · CP11.4a.2 `new_structure` 耐久 import 发现/登记与 license 来源

- date: 2026-07-11
- commit: `43dd99efcdc2469e8f96124695381a6287b19b6f` — feat: close CP11.4a.2 durable import discovery
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4a.2（属：步⑪ CP11.4 残余架构边界；CP11.4a 父项因三闸来源尚未全部闭合而继续保持未完成）

## 决策

本检查点闭合 `new_structure` 类型闸的生产只读发现链，不把全部 import 触发条件或不可信代码物化伪装成完成。
`human_named` 仍须结构化人类指令来源，`sota_reference` 仍须冻结论文/benchmark 来源，`stuck` 只能触发外部普查并新建
idea/question，不能直接复用原问题发 `import_defer`；这些来源契约拆到 CP11.4a.3。

- plan 在本 action-cycle 无候选且 type gate=`new_structure` 时，可单独返回一个经 schema 限制的
  `import_search_request.json`；它是 control sidecar，不是 Gate 研究事实。sidecar 不得与 plan 或其他文件共存，
  同一 action-cycle 最多消费一次，模型不能直接写 candidate/license 权威事实。
- `AttackStages` 先以私有权限和 fsync 持久化请求，再调用受信 host connector；返回后重渲染包含冻结候选的 plan pack，
  用新 plan session 继续生成。零结果也写耐久 completion marker 并展示给下一次 plan，防止重启后重复搜索或循环请求。
- 默认 connector 使用有界 GitHub REST 只读 GET：按 stars 排序检索 repository、解析精确 40 位 commit、在 pinned ref
  读取 license 并哈希内容；redirect 只允许 `api.github.com`。可选 `METARESEARCH_GITHUB_TOKEN` 只进入请求 header，
  不写 policy、receipt、DB 或日志。
- `ImportSearchService` 在网络前创建 durable `runner_call(created/running)` 和逐 invocation 私有 receipt；网络调用不持
  SQLite 写事务。成功后以一个短事务原子登记 candidate、license review、runner terminal/cost ledger 与 completion
  decision。receipt 已存在而 DB 未完成时只 finalize、不重新 GET；无 receipt 的 orphan 可重做只读 GET，但 DB 登记仍幂等。
- candidate 固定 URI/revision/license id/search provider/query；license evidence 固定内容哈希和来源。默认 auto policy
  仅允许 Apache-2.0/MIT/BSD-2-Clause/BSD-3-Clause/ISC，scope 为 eval/modify/publish=true、redistribute=false；
  非 allowlist 只记 `review`，不会被选择。人类 license decision 必须带 actor/evidence provenance。
- 检索档位由 question score、`est_cost/B(t)`、policy thresholds 和 candidate budget 机械计算；compiler 明示
  `search_completed`、`may_request_import_search` 与零结果状态，并交叉核 completion/candidate/license/runner receipt。
- 新 provenance 行使用 v3 candidate/license snapshot hash；历史字段全为 NULL 的冻结行继续使用原 v2 公式，保证旧
  work-root、旧 `import_defer` 和恢复锚不漂移。receipt `version` 显式拒绝 bool。
- 冻结 migration 已包含新增 provenance 列，`runner_call.phase`/`decision.type` 也是开放 TEXT，因此本检查点没有 DDL
  迁移。默认 materializer 仍因缺少 adversarial sandbox 而 fail-closed；本提交只使“发现→登记→可调度”生产可达。

## 改动文件

- `meta-research/README.md` — 记录 GitHub provider/token 配置、`new_structure` 自动发现链及尚未安全物化的边界。
- `meta-research/orchestrator/__init__.py` — 将 `import_search` 模块纳入组件索引并校准 import worker 描述。
- `meta-research/orchestrator/attack_stages.py` — 消费唯一 search sidecar、请求先落盘、调用后重渲染并换 plan session；
  恢复零结果和拒绝同轮二次请求。
- `meta-research/orchestrator/compiler_sqlite.py` — 投影三闸资格、搜索完成/零结果状态和候选冻结摘要，机械核验 completion 数量。
- `meta-research/orchestrator/gate.py` — Stub Gate 显式把 import search control sidecar 拒在研究事实之外。
- `meta-research/orchestrator/gate_sqlite.py` — SQLite Gate 同步 control sidecar 隔离规则。
- `meta-research/orchestrator/import_search.py` — 新增有界 GitHub connector、私有 receipt、runner/cost 生命周期、原子登记、
  重启 finalize 与 auto/human license provenance 的生产服务。
- `meta-research/orchestrator/importer.py` — 新增事务内 candidate/license 登记 helper、provenance 字段投影及 v2/v3 hash 兼容。
- `meta-research/orchestrator/run.py` — 默认惰性装配 GitHub provider，并保留可注入确定性 provider 的测试/部署边界。
- `meta-research/orchestrator/schemas.py` — 说明 import search 是独立 schema control sidecar，不加入 Gate artifact map。
- `meta-research/orchestrator/stage_provider.py` — 校验 standalone request sidecar，拒绝与 plan/其他文件共存并计 runner usage/cost。
- `meta-research/policies/policy.yaml` — 增加机械 scale thresholds、provider 限额和冻结 auto-license allowlist/scope。
- `meta-research/prompts/skills/plan/SKILL.md` — 规定仅 `new_structure` 可在无候选时发一次 search request；三类未闭来源 fail-closed。
- `meta-research/schemas/import_search_request.schema.json` — 新增 version 1、仅 `new_structure`、有界 query/need summary 的封闭 schema。
- `meta-research/schemas/policy.schema.json` — 机械校验 import search provider、限额、档位阈值和 license policy。
- `meta-research/tests/fixtures/invalid/import_search_request/stuck_direct.expect` — 固定 `stuck` 直接请求必须失败的期望。
- `meta-research/tests/fixtures/invalid/import_search_request/stuck_direct.json` — 提供非法直接 `stuck` sidecar 负例。
- `meta-research/tests/fixtures/valid/import_search_request/new_structure.json` — 提供合法 `new_structure` sidecar 样例。
- `meta-research/tests/test_attack_advance.py` — 覆盖搜索→重渲染→四锚 plan、零结果、同轮二次请求与恢复路径。
- `meta-research/tests/test_import_search.py` — 覆盖 provider 限额/redirect/token 隔离、receipt 崩溃缝隙、原子登记、
  auto/human license、腐化 fail-loud、scale 和 completion 对账。
- `meta-research/tests/test_isolation_m1c.py` — 覆盖 provenance v3 hash、旧行精确 v2 兼容与 license evidence。
- `meta-research/tests/test_orchestrator.py` — 固定 import search sidecar 不得进入 Gate artifact map。
- `meta-research/tests/test_run.py` — 覆盖默认 provider 惰性装配、确定性注入和生产链可达性。
- `meta-research/tests/test_schemas.py` — 覆盖 request/policy schema 正反例与三闸拒绝。
- `meta-research/tests/test_stage_provider.py` — 覆盖 standalone sidecar、共存拒绝和调用成本记账。

## Review（codex-chatgpt gpt-5.5/xhigh）

- 第 1 轮：`codexro-review` 在出 verdict 前因独立账号 401 `token_invalidated` 失败；证据目录
  `/tmp/codexrev.RxWlrV/`，没有把基础设施失败冒充模型批准。
- 第 2 轮（上限）：内联完整 staged diff 只读审查，结论 `REQUEST_CHANGES`（1 BLOCKER、2 SHOULD、1 NIT），
  输出 `/tmp/review_cp114a2_r2.md`。
  - 采纳并修复：新 provenance hash 升 v3 会破坏历史 durable anchor，改为旧 NULL provenance 行保持精确 v2；
    receipt `version=True` 会因 Python bool/int 等价被接受，增加显式拒绝。补 exact regression 后 20 passed。
  - 未采纳 BLOCKER“缺 DDL/枚举迁移”：逐项核对冻结 migration，`external_candidate`/`license_review` 已有这些列，
    `runner_call.phase` 与 `decision.type` 为无 CHECK 的 TEXT；新增无效迁移反而会制造 schema 漂移。
  - 未采纳 SHOULD“所有 finalize 错误都转失败并重规划”：剩余分支是 receipt/DB 腐化或不变量冲突，必须保持
    unresolved/fail-loud，不能掩盖审计损坏后再发非确定性外调；只有 trigger identity 改变会 durable failed+retry。
- 已到两轮上限，不发第 3 轮；成立项全部修复，误报和不适用建议均留有可复核理由。

## 验证

- 开发期相关验证：
  - 主相关组 → **`358 passed in 93.76s`**。
  - schema/service/stage/orchestrator 组合 → **`158 passed in 14.23s`**。
  - 外审修复 exact 组 → **`20 passed, 87 deselected in 8.10s`**。
  - completion 对账后的 schema/import 组合 → **`91 passed in 4.86s`**；skills/frozen 组合另有 8 项通过。
- 提交边界只运行一次全量：`pytest -q` → **`1297 passed in 301.64s (0:05:01)`**；无失败、未运行第二次全量。
- `compileall`/`py_compile`、`git diff --check`、`git diff --cached --check` 均通过。

## 遗留 / 回退

- CP11.4a 仍未完成：`human_named` 的结构化 directive provenance、`sota_reference` 的冻结论文/benchmark snapshot，
  以及 `stuck` 普查只能产新 idea/question 的来源协议留 CP11.4a.3。
- CP11.4b 仍需 artifact capability/fd-safe 消费和 provider invocation/usage/billing exactly-once；CP11.4c 仍需
  container/cgroup/VM 等敌对隔离、跨节点 VEPFS 验收和含真实外调/失败注入的 100+ 轮 soak。
- 回退前停止 orchestrator 并确认无 `phase=import_search` running call，备份 DB 与 receipt 目录；执行
  `git revert 43dd99efcdc2469e8f96124695381a6287b19b6f`。本提交无 DDL migration，但旧代码不识别新 control sidecar、
  completion decision 和 v3 provenance，不应在搜索 finalize 在途时热回退。
