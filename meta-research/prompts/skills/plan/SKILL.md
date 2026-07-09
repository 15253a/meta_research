# SKILL · plan —— 复用判定 + 锁评估协议

> 版本：m0-1。按《第一部分》§3.3 与流程图 04-Plan；产物 schema = `schemas/plan.schema.json`。
> 本阶段 = 计划调用（phase=plan）+ **可回答性评审**独立调用（phase=audit，≤2 轮，本文件
> 【评审任务】节）。评估协议在本阶段锁死，bundle 只照办、不再发明（I2 源头）。

## 通用

- **触发条件**：idea 阶段产出 selected idea 后。
- **读取**：normalized selected idea（candidate_id / core_claim / mechanism / assumptions /
  min_falsifiable_experiment / audit_mapping / novelty_refs）· 单轮预算 B(t) · 依赖基线卡 ·
  召回的三层池卡与相似协议卡（M0 池空 → 检索区为空属正常）。
- **门禁与写入**：plan.json 经 gate 落 protocol + 占坑 + build_target 队列 + required_metric。
- **失败语义**：评审两轮不过 → 阶段失败 = 研究轮正常收尾（cycle=done、Qn inconclusive、
  visit+1）汇入 reasoning；**无从设计实验 = idea 不合格**，同样收尾、创造性回流上游（§3.2.5）。

## 【计划任务】（phase=plan）

产出 `files["plan.json"]`。步骤：

1. **拆 verification needs**：从 selected idea 的 `assumptions` 与 `min_falsifiable_experiment`
   派生（每 need 一句可判定命题；来源标 source）。**协议/指标/target 由你依本 skill 重新推导**，
   不受 idea 文本约束。特例：父问题**聚合轮**（全部子依赖 satisfied、以 child_answer 关闭）
   不产生验证需求——`needs=[]`、`targets=[]`，reuse_evidence 逐条引子问题 answer（kind=child_answer），
   此时 protocol/metric_defs/readout_rules 可省。
2. **逐 need 机械复用判定**（§3.3.1 五情形；判定依据只能是检索区给出的池事实，M0 池空即"池中无"）。
   **情形 → 处置 → target 必带字段**（照表写、缺一即被 schema 拒）：

   | 情形 | 处置 | eval_action | attempt_purpose | 另须携带 |
   |---|---|---|---|---|
   | 同协议@版本+指标已测+env 兼容 | 引历史 evaluation 进 `reuse_evidence`，**不产 target** | — | — | — |
   | 协议场景升版 | `eval` target（新 evaluation） | `create_evaluation` | `protocol_upgrade` | `eval_key` + `evaluation_source="protocol_upgrade"` |
   | 同协议@版本缺指标 | `eval` target（追加 attempt） | `append_attempt` | `metric_append` | `evaluation_id`（既有格子） |
   | env 失配 / 结果存疑 | `eval` target（identity 复现） | `append_attempt` | `repro_eval` | `evaluation_id` |
   | 池中无 + 自建角色（消融/替换/超参/评估） | `build` / `exec` / `eval`（身份按三问决策树 §1.2.2：前向逻辑变=新 baseline、要重训=新 variant、只改评估=新 evaluation） | （eval 时按上表） | （eval 时按上表） | **build ⇒ `claim{canonical_key, slug}`；exec ⇒ `claim{baseline_ref, variant_key, config_json}`**（I5 占坑输入） |

   ⚠️ **kind 前置条件（选错即整轮被拒）**：`exec` 的 `baseline_ref` 与 `eval` 的评估对象都**必须指向
   检索区已有的 legal 池资产**——检索区没有该家族的 legal baseline（含池空）时，`exec`/`eval` 语义非法，
   **必须走 `build` 先建 baseline**（首攻新家族恒为 build）。若上下文包给出「上轮 plan 被拒原因」，
   先修正该原因再产出本轮 plan。
   | 池中无 + 需引入外部 baseline 家族/公认参照 | **M0 不走 import**：能自建对照则改自建；否则该 need 记入 md 正文"本轮无法覆盖"，由轮尾 reasoning 裁决（M1–M3 起此分支写 `import_defer`；M0 不产该字段） | — | — | — |
3. **锁评估协议**（字段名按 schema 逐字写）：
   - `protocol`：`{name, version（整数≥1）, scope_spec（对象：评估数据/分割/checkpoint 选择/
     流程——一字一句都是 I1 冻结对象）, smoke_md?（smoke 定义）}`；
   - `metric_defs[]`：每项 `{metric_id, version（整数）, name, direction ∈ {higher,lower},
     compute_spec_md, unit?}`；
   - `readout_rules[]`：每项 `{metric_id, metric_ver, rule_md}`（每指标必有判读）；
   - 每 target `budget_estimate`（总和 ≤ B(t)）。
4. **写 targets 队列**——每个 target 的完整必填字段（schema 硬性要求）：
   `target_key`（plan 内稳定键，如 "t1"，bundle 产物与 required_metric 以此关联）·
   `target_kind ∈ {build,exec,eval}` · `seq`（依赖序，从 1 起）· `critical`（true=失败即早退）·
   `budget_estimate`（数值，总和 ≤ B(t)）· `spec_md`（本目标做什么，bundle 只照办）·
   按 kind 另加上表"另须携带"列（build/exec 的 `claim`、eval 的三件套）。
   `build_target_required_metric` 逐 target 声明 required 指标集
   （`{target_key, metric_id, metric_ver}`，I2 核覆盖依据）。
5. **依赖等待分支**（图 04 DEP 判断）：所需 baseline 正被他轮 building（占坑互斥 I5，
   检索区基线卡会标 building）→ 不重复开工：在 `md` 里声明「等待 <baseline> 就绪」，
   由编排器写 `question_dep(dep_type=baseline, pending)` 并把本轮收成 **dependency_wait**
   （机械收尾：写 dep + 释放 Qn 回 open + mark_cycle_done，不经 reasoning，§4.2.5）。
   M0 单驱动器串行、通常不会触发，契约仍在此声明。
6. `md` 写计划正文（中文）：needs 表、复用判定逐条结论、协议锁定理由、预算分配。

**输出骨架（键名逐字，封闭对象；`<>` 占位、`?` 表可省键）**：

```json
{ "needs": [ { "need_id": "n1", "statement_md": "<>", "source?": "<assumptions|min_falsifiable_experiment|other>" } ],
  "reuse_evidence": [ { "need_id?": "n1", "kind": "<evaluation|child_answer>", "ref_md": "<>",
                        "evaluation_id?": "<>", "metric_result_id?": "<>", "answer_id?": "<>" } ],
  "targets": [
    { "target_key": "t1", "target_kind": "<build|exec|eval>", "seq": 1, "critical": <true|false>,
      "budget_estimate": <数值>, "spec_md": "<bundle 只照办的执行说明>", "need_ids?": ["n1"],
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

骨架纪律：顶层只许上列七键（第八种可能键 import_defer M0 不产）；target 内不加自造键；
聚合轮：needs/targets 均为 []、protocol/metric_defs/readout_rules **整体省略**，但
`reuse_evidence`（child_answer 证据逐条）与 `build_target_required_metric`（=[]）**仍必须在场**。
**scope_spec 只写评估场景**（评估数据/分割/checkpoint 选择/评估流程）——训练配置的唯一
载体是 target 的 `claim.config_json`，**不得在协议里双写训练配置**（双写必然漂移、评审打回）。

## 【评审任务】（phase=audit，独立会话，≤2 轮）

输入 = plan.json + normalized selected idea（**不含计划推理过程**）。
产出 `files["plan_review.json"]`：`{ "verdict": "pass"|"fail", "issues": [ {"item": "...",
"why": "...", "fix_hint": "..."} ], "round_no": N }`。逐条核验（可回答性清单，图 04）：

1. 每个 need ≥1 个声明指标覆盖（对照 build_target_required_metric 与 reuse_evidence）；
2. 每个指标有判读规则（readout_rules 齐）；
3. 每个 build/exec target 有 smoke 定义与预算；
4. 指标全部由协议声明（metric_defs ⊆ 协议范围；I2 源头）；
5. targets 依赖序自洽（seq、eval 引用的对象在此前产生或已在池中）。

fail → 编排器把 issues 回传计划会话修一轮再评；第 2 轮仍 fail → 阶段失败（见通用·失败语义）。
