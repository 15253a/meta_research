# SKILL · plan —— 复用判定 + 锁评估协议

> 版本：m4-cp114c2b3b。按《第一部分》§3.3 与流程图 04-Plan；产物 schema =
> `schemas/plan.schema.json` 或受限控制 sidecar `schemas/import_search_request.schema.json`。
> 本阶段 = 计划调用（phase=plan）+ **可回答性评审**独立调用（phase=audit，≤2 轮，本文件
> 【评审任务】节）。评估协议在本阶段锁死，bundle 只照办、不再发明（I2 源头）。

## 通用

- **触发条件**：idea 阶段产出 selected idea 后。
- **读取**：normalized selected idea（candidate_id / core_claim / mechanism / assumptions /
  min_falsifiable_experiment / audit_mapping / novelty_refs）· 单轮预算 B(t) · 依赖基线卡 ·
  召回的三层池卡与相似协议卡；本 action-cycle 已登记的 external candidate/license 冻结摘要（可能为空）。
- **门禁与写入**：plan.json 经 gate 落 protocol + 占坑 + build_target 队列 + required_metric。
- **失败语义**：评审两轮不过 → 阶段失败 = 研究轮正常收尾（cycle=done、Qn inconclusive、
  visit+1）汇入 reasoning；**无从设计实验 = idea 不合格**，同样收尾、创造性回流上游（§3.2.5）。

## 【计划任务】（phase=plan）

每次调用二选一：产出 `files["plan.json"]`，或在下述精确条件下**只产**
`files["import_search_request.json"]`。搜索 sidecar 不能与 plan.json/其他文件共存；编排器完成受信
只读搜索与登记后，会用**全新无状态 plan 会话**给出重渲染的冻结候选锚。步骤：

1. **拆 verification needs**：从 selected idea 的 `assumptions` 与 `min_falsifiable_experiment`
   派生（每 need 一句可判定命题；来源标 source）。**协议/指标/target 由你依本 skill 重新推导**，
   不受 idea 文本约束。特例：父问题**聚合轮**（全部子依赖 satisfied、以 child_answer 关闭）
   不产生验证需求——`needs=[]`、`targets=[]`，reuse_evidence 逐条引子问题 answer（kind=child_answer），
   此时 protocol/metric_defs/readout_rules 可省。
2. **逐 need 机械复用判定**（§3.3.1 五情形；判定依据只能是固定锚/检索区给出的已提交事实，池空即"池中无"）。
   **情形 → 处置 → target 必带字段**（照表写、缺一即被 schema 拒）：

   | 情形 | 处置 | eval_action | attempt_purpose | 另须携带 |
   |---|---|---|---|---|
   | 同协议@版本+指标已测+env 兼容 | 引历史 evaluation 进 `reuse_evidence`，**不产 target** | — | — | — |
   | 协议场景升版 | `eval` target（新 evaluation） | `create_evaluation` | `protocol_upgrade` | `eval_key` + `evaluation_source="protocol_upgrade"` |
   | 同协议@版本缺指标 | `eval` target（追加 attempt） | `append_attempt` | `metric_append` | `evaluation_id`（既有格子） |
   | env 失配 / 结果存疑 | `eval` target（identity 复现） | `append_attempt` | `repro_eval` | `evaluation_id` |
   | 池中无 + 自建角色（消融/替换/超参/评估） | `build` / `exec` / `eval`（身份按三问决策树 §1.2.2：前向逻辑变=新 baseline、要重训=新 variant、只改评估=新 evaluation） | （eval 时按上表） | （eval 时按上表） | **build ⇒ `claim{canonical_key, slug}`；exec ⇒ `claim{baseline_ref, variant_key, config_json}`**（I5 占坑输入） |
   | 池中无 + 需引入独立外部 baseline 家族 | 先读固定锚的 external import 状态。`may_emit_import_defer=true` 时才产顶层 `import_defer`、`targets=[]`，逐字照抄四锚；候选/license 由编排器事务重算，**不得自造 URI、candidate id 或 hash**。候选为空时只能选择固定锚中值为 true 的一个 `may_request_*` / `may_activate_*` 分支，并只产一次下方对应 sidecar。`search_completed=true` 后无论零结果/无 allow 候选都不得再搜：能自建则 build，否则诚实产零 target 计划。 | — | — | — |

   ⚠️ **kind 前置条件（选错即整轮被拒）**：`exec` 的 `baseline_ref` 与 `eval` 的评估对象都**必须指向
   检索区已有的 legal 池资产**——检索区没有该家族的 legal baseline（含池空）时，`exec`/`eval` 语义非法，
   **必须走 `build` 先建 baseline**（首攻新家族恒为 build）。若上下文包给出「上轮 plan 被拒原因」，
   先修正该原因再产出本轮 plan。
3. **import 三闸与搜索 sidecar 边界**：
   - `may_request_import_search=true`：类型门 `new_structure`，只提交 query+需求摘要；当前题命中 stuck 时该位必为 false。
   - `may_request_stuck_survey=true`：只读普查。该位由编排器同时核对 lifetime visit 与当前 goal version 的
     append-only consecutive-inconclusive 账本，不得从文本自行推断。服务只会给**新参照问题**冻结来源并让原题
     依赖它；原题绝不登记 candidate、绝不产 import_defer。只可用 `trigger_kind="stuck"` 的 query 骨架。
   - `may_request_sota_reference=true`：须给出明确 paper/benchmark HTTPS URI + 实现检索词；受信 host 先下载并
     内容寻址冻结来源，再派生独立 baseline-reference 问题。URI 不是权威，成功冻结的 blob/hash 才是。
   - `may_activate_source_authority=true`：当前题已经有受信 authority。逐字照抄固定锚的 trigger_kind、
     source_authority_hash、need_summary；不得再给 query/URI。`human_named` 只能走此分支，且 authority 必来自已确认
     的结构化 `inject_question` directive；`stuck/sota_reference` 子题从父轮冻结 receipt 激活，不重新联网。
   - 任一分支都不得上报 repo revision/rank/license/SPDX/授权范围；不得借 `new_structure` 冒充其他直达来源。
   - 候选数与检索强度由编排器按 question score/est_cost + B(t) + policy 机械决定，
     所以 sidecar 中没有也不得自造“搜几个”字段。
4. **锁评估协议**（字段名按 schema 逐字写）：
   - `protocol`：`{name, version（整数≥1）, scope_spec（对象：评估数据/分割/checkpoint 选择/
     流程——一字一句都是 I1 冻结对象）, smoke_md?（smoke 定义）}`；
   - `metric_defs[]`：每项 `{metric_id, version（整数）, name, direction ∈ {higher,lower},
     compute_spec_md, unit?}`；
   - `readout_rules[]`：每项 `{metric_id, metric_ver, rule_md}`（每指标必有判读）；
   - 每 target `budget_estimate`（总和 ≤ B(t)）。
5. **写 targets 队列**——每个 target 的完整必填字段（schema 硬性要求）：
   `target_key`（plan 内稳定键，如 "t1"，bundle 产物与 required_metric 以此关联）·
   `target_kind ∈ {build,exec,eval}` · `seq`（依赖序，从 1 起）· `critical`（true=失败即早退）·
   `budget_estimate`（数值，总和 ≤ B(t)）· `gpu_required`（bool；只有代码/评估确需 CUDA 时为 true）·
   `spec_md`（本目标做什么，bundle 只照办）·
   按 kind 另加上表"另须携带"列（build/exec 的 `claim`、eval 的三件套）。
   `build_target_required_metric` 逐 target 声明 required 指标集
   （`{target_key, metric_id, metric_ver}`，I2 核覆盖依据）。
6. **依赖等待分支**（图 04 DEP 判断）：所需 baseline 正被他轮 building（占坑互斥 I5，
   检索区基线卡会标 building）→ 不重复开工：在 `md` 里声明「等待 <baseline> 就绪」，
   由编排器写 `question_dep(dep_type=baseline, pending)` 并把本轮收成 **dependency_wait**
   （机械收尾：写 dep + 释放 Qn 回 open + mark_cycle_done，不经 reasoning，§4.2.5）。
   import 三闸命中且固定锚允许时，输出 `import_defer`（不得同时有 target/protocol/metric）；编排器在一个
   plan phase_commit 中机械写选择事件 + 占位 baseline + pending dep + `dependency_wait` route，释放 Qn 并
   收尾，不进普通 plan 可回答性评审、bundle/reasoning（图 04 IMP→WAIT 发生在 PROTO/REVIEW 之前）。
   物化成功、dep satisfied 后同一 Qn 重新进入 attack；物化终败则编排器写失败裁决并把 exact dep 置
   `blocked`，同一 Qn 回到可重规划集合，下一轮必须消费失败摘要后改候选/自建/分解，禁止原样死循环。
7. `md` 写计划正文（中文）：needs 表、复用判定逐条结论、协议锁定理由、预算分配。

**输出骨架（键名逐字，封闭对象；`<>` 占位、`?` 表可省键）**：

**import_search_request 专用骨架**（四选一，必须与固定锚的 true 分支精确对应；独占 files，产出后立即结束）：

```json
{ "version": 1,
  "trigger_kind": "new_structure",
  "query": "用于找到对应实现的简短 GitHub 代码检索词",
  "need_summary": "为什么当前 verification need 需要独立外部 baseline 家族" }
```

```json
{ "version": 1,
  "trigger_kind": "stuck",
  "query": "只读外部普查检索词",
  "need_summary": "为什么应派生一个独立外部参照问题" }
```

```json
{ "version": 1,
  "trigger_kind": "sota_reference",
  "query": "冻结参照所对应实现的 GitHub 检索词",
  "need_summary": "为什么需要独立 SOTA baseline-reference 问题",
  "reference": { "kind": "paper", "uri": "https://<固定来源>" } }
```

```json
{ "version": 1,
  "trigger_kind": "<human_named|stuck|sota_reference>",
  "source_authority_hash": "<逐字照抄 source_authority.source_authority_hash>",
  "need_summary": "<逐字照抄 source_authority.need_summary>" }
```

`文件名 = import_search_request.json`，不是 plan.json；除 SOTA 的 frozen-source 请求外不加 repo/reference URL；
所有分支都不加 max_candidates/provider/license 等键。

**普通 plan 骨架**：

```json
{ "needs": [ { "need_id": "n1", "statement_md": "<>", "source?": "<assumptions|min_falsifiable_experiment|other>" } ],
  "reuse_evidence": [ { "need_id?": "n1", "kind": "<evaluation|child_answer>", "ref_md": "<>",
                        "evaluation_id?": "<>", "metric_result_id?": "<>", "answer_id?": "<>" } ],
  "targets": [
    { "target_key": "t1", "target_kind": "<build|exec|eval>", "seq": 1, "critical": <true|false>,
      "budget_estimate": <数值>, "gpu_required": <true|false>,
      "spec_md": "<bundle 只照办的执行说明>", "need_ids?": ["n1"],
      "claim?": { "canonical_key?": "<build 必>", "slug?": "<build 必>",
                  "baseline_ref?": "<exec 必>", "variant_key?": "<exec 必>", "config_json?": {} },
      "eval_action?": "<create_evaluation|append_attempt>", "attempt_purpose?": "<按情形表>",
      "eval_key?": "<create 必>", "evaluation_source?": "<create 必>", "evaluation_id?": "<append 必>" } ],
  "protocol": { "name": "<>", "version": 1, "scope_spec": { "<场景字段自定>": "<>" }, "smoke_md?": "<>" },
  "metric_defs": [ { "metric_id": "<>", "version": 1, "name": "<>",
                     "direction": "<higher|lower>", "compute_spec_md": "<>", "unit?": "<>" } ],
  "readout_rules": [ { "metric_id": "<>", "metric_ver": 1, "rule_md": "<>" } ],
  "build_target_required_metric": [ { "target_key": "t1", "metric_id": "<>", "metric_ver": 1 } ] }
```

骨架纪律：普通 plan 顶层只许上列七键；`import_defer` 只按下方专用骨架出现；target 内不加自造键；
聚合轮：needs/targets 均为 []、protocol/metric_defs/readout_rules **整体省略**，但
`reuse_evidence`（child_answer 证据逐条）与 `build_target_required_metric`（=[]）**仍必须在场**。
**scope_spec 只写评估场景**（评估数据/分割/checkpoint 选择/评估流程）——训练配置的唯一
载体是 target 的 `claim.config_json`，**不得在协议里双写训练配置**（双写必然漂移、评审打回）。

**import_defer 专用骨架**（与上方普通 plan 二选一）：

```json
{ "needs": [], "reuse_evidence": [], "targets": [], "build_target_required_metric": [],
  "import_defer": {
    "reason_md": "为什么该问题需要已登记的外部基线",
    "candidate_set_hash": "<逐字照抄 anchors.candidate_set_hash>",
    "license_decision_snapshot_hash": "<逐字照抄 anchors.license_decision_snapshot_hash>",
    "selection_key": "<逐字照抄 anchors.selection_key>",
    "policy_hash": "<逐字照抄 anchors.policy_hash>",
    "placeholder_baseline_identity": {
      "canonical_key_draft": "<稳定方法族键>", "slug_draft": "<稳定可读 slug>",
      "identity_md": "<中文占位身份；只描述用途，不宣称已物化/已验证>"
    }
  }
}
```

## 【评审任务】（phase=audit，独立会话，≤2 轮）

本任务只审普通实验 plan；`import_defer` 专用 plan 不调用本任务。

输入 = plan.json + normalized selected idea（**不含计划推理过程**）。
产出 `files["plan_review.json"]`：`{ "verdict": "pass"|"fail", "issues": [ {"item": "...",
"why": "...", "fix_hint": "..."} ], "round_no": N }`。逐条核验（可回答性清单，图 04）：

1. 每个 need ≥1 个声明指标覆盖（对照 build_target_required_metric 与 reuse_evidence）；
2. 每个指标有判读规则（readout_rules 齐）；
3. 每个 build/exec target 有 smoke 定义与预算；每个 target 显式写 `gpu_required`，且与执行说明是否使用
   CUDA/GPU 一致；
4. 指标全部由协议声明（metric_defs ⊆ 协议范围；I2 源头）；
5. targets 依赖序自洽（seq、eval 引用的对象在此前产生或已在池中）。

fail → 编排器把 issues 回传计划会话修一轮再评；第 2 轮仍 fail → 阶段失败（见通用·失败语义）。
