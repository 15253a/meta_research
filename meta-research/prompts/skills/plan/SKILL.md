# SKILL · plan —— 复用判定、对象身份与协议锁定

plan 把选中的 idea 变成最小可证伪实验与最小可执行计划。对象身份、协议和判读规则由 Codex 自主决定；
它是研究语义的 owner；编排器只能机械核验、解析引用、占坑和执行，
不得替你生成 baseline/variant/evaluation 的研究身份，也不得替你发明泛化 readout。
正常路径由一个 Plan 常驻主智能体在一个连续 turn 内完成草稿、一次干净子评审、
`preflight_plan` 只读预检、修订与最终提交；不把任何格式/冲突错误交给外层重新调用模型。

## 触发条件与读取

- 触发条件：reasoning 已选定一个研究 question 及其候选 idea，并将本轮推进到 plan。
- 读取：当前 Qn、selected idea、单轮预算、可信执行身份，以及 ContextPack 中的 legal 池/协议/成功测量候选。
- 【计划任务】：按下述三问判定 build/exec/eval 或严格复用，锁定最小实验的研究语义。
- 输出：无论是否需要受控外部发现，最终都只提交通过
  `schemas/plan.schema.json` 的 `files["plan.json"]`。外部发现是当前主 turn
  中的 `plan_import_search` 工具调用，不是阶段产物或退出 sidecar。
- 失败：无法形成可回答计划时诚实失败，交 reasoning 收为 inconclusive；不得用目录盘点、修代码、格式检查等
  工程步骤冒充研究 target 或新 question。

## 三问决策树（逐 verification need 执行）

先从 idea 的 assumptions 与 min_falsifiable_experiment 拆出 `needs[]`，每项必须是可由一次测量或合法复用
判定的命题。`needs[].source` 的封闭枚举必须逐字使用
`assumptions | min_falsifiable_experiment | other`；注意是复数 `assumptions`，不得写单数
`assumption`。随后对每个 need 依次回答：

1. 前向逻辑或算法结构是否变化？是 → 新 `baseline`，用 `target_kind="build"`，并由你给稳定、语义化的
   `claim.canonical_key` 与 `claim.slug`。禁止 `auto-cN-tN`、轮次号或目录任务充当研究身份。
2. 结构不变时，是否必须重新训练或产生新的可评对象？是 → 既有 legal baseline 下的新 `variant`，用
   `target_kind="exec"`，声明 `baseline_ref + variant_key + config_json`。训练数据、切分、超参和训练流程
   属 variant 配置，不属于 baseline，也不得塞进 evaluation 身份。随机 seed 是执行 replicate 的事实：
   单 seed 的 build/exec target 用 `replicate={"seed":...}` 冻结，并由编排器写入 durable `run.seed`；
   不得把任何层级的 `seed` 塞进 `variant_key` 或 `config_json`。旧 Plan 省略 `replicate` 仅按
   `seed=null` 兼容。
3. 结构与训练产物均不变，只改变评估数据、协议、指标或重测吗？是 → `target_kind="eval"`：新协议格子用
   `create_evaluation`；同一格补指标/复现/重试用 `append_attempt`。evaluation 是对照元，不能登记为 baseline。

### eval 目标的封闭字段合同

`eval` 的动作字段与来源字段不是同一个枚举，必须按下面的完整形状一次写对：

- 新建评估格：`eval_action="create_evaluation"`，同时必填
  `attempt_purpose + eval_key + evaluation_source + claim`；其中
  `claim={"baseline_ref":"...","variant_key":"..."}`，不得使用 build 的
  `canonical_key/slug`。三种常用且无歧义的配对是
  `factory/factory`、`protocol_upgrade/protocol_upgrade`、
  `standalone_eval/standalone_eval`（前者是 `attempt_purpose`，后者是
  `evaluation_source`）。绝不能把 `create_evaluation` 写进
  `evaluation_source`，也不能省略 `eval_action`。
- 追加既有格：`eval_action="append_attempt"`，同时必填已有的
  `evaluation_id` 和 `attempt_purpose`；用途按事实从
  `retry | metric_append | repro_eval` 中选择。不要为 append 自造
  `eval_key/evaluation_source` 或 baseline 身份。

```json
{
  "target_key": "eval-published-v2",
  "target_kind": "eval",
  "seq": 1,
  "critical": true,
  "budget_estimate": 0.5,
  "gpu_required": true,
  "spec_md": "按升版协议重评已发布 checkpoint",
  "need_ids": ["n1"],
  "eval_action": "create_evaluation",
  "attempt_purpose": "protocol_upgrade",
  "eval_key": "lodo-v2",
  "evaluation_source": "protocol_upgrade",
  "claim": {"baseline_ref": "published-family", "variant_key": "base"}
}
```

`create_evaluation` 只能引用 ContextPack 中已经 `legal`、已正式发布且有 checkpoint 的
baseline/variant，不能引用同一份 plan 里的 `build/exec` target。新结构的出厂评估已经由各自
`build/exec` target 承担：把该结构所需指标逐项挂入它自己的
`build_target_required_metric`。若多个本轮新结构还需要额外的跨结构评估格，本轮先分别 build 并完成
各自出厂测量，待它们合法发布后在后续 cycle 再建 comparison evaluation；不得凭空增加一个引用
同轮新身份的 eval target。最小可执行的分阶段计划优先于机械照抄一个后端无法解析的评审建议。

## 机械复用判定

ContextPack 的池目录只是候选，不是复用裁决。你必须先锁定所需的 protocol@version、required metrics 和
CPU/GPU workload 模式，再按以下五项逐条核对：

- baseline 与 variant 都是 `legal`，对象身份与当前 need 精确相符；
- protocol@version 的 scope 与当前计划精确相同；
- 同一成功 evaluation/attempt 覆盖全部 required `(metric_id, metric_ver)` 的 aggregate 结果；
- 预期 workload env 精确相等；
- attempt 非 parser-suspect，且来源 build target（若有）为 complete。

只有命中时才能省略 target，并在 `reuse_evidence` 逐 need 填真实 `evaluation_id + metric_result_id +
gpu_required`。编排器会用规范 selector 对 DB 真相重算；自由文本 `ref_md` 不授予复用权。父问题聚合仅可引用
直接子问题的 `answer_id`。任一 required 缺失、env 不同或结果存疑都必须落 `eval/exec/build`，不得错误复用。

## 协议与指标锁定

- `protocol={name,version,scope_spec,smoke_md?}`。scope_spec 只含评估场景：数据选择、分组/折切分、防泄漏边界、
  checkpoint 选择、重复/seed 与评估流程；场景或指标集合变化必须升 version。
- `metric_defs[]` 每项完整给 `metric_id,version,name,direction,compute_spec_md`；不得把 smoke、文件存在、日志格式
  或目录齐备当研究指标。
- `readout_rules[]` 逐指标给可回答问题的判读规则。编排器不得从 higher/lower 自动生成规则。
- `build_target_required_metric[]` 必须逐 target 精确声明；不要把所有指标机械铺到所有 target。
- 每个新 target 必须给 `scientific_contract`。`validity_gates` 必须各一次包含
  `required_metrics_present`、`parser_not_suspect` 和
  `independent_code_plan_data_boundary_review_receipt_present`。它们分别绑定 required 指标覆盖、
  parser 健康，以及代码写完后由独立子智能体对代码↔Plan 一致性和数据边界所做审查的耐久回执。第三项只表示
  独立审查确实发生并绑定了当前代码，不得表述为运行时已经证明“绝无泄漏”。三者都是 Bundle 的硬有效性门，
  任一失败都禁止正式发布且直接交 Reasoning，不得用高分指标覆盖。`outcome_rules` 只能引用该 target 已声明的 required
  metric/version；规则只把有效实验分类为 `supported | refuted | inconclusive`。零条规则或多规则分类冲突
  均得到 `inconclusive`，仍是可入池、可复用的有效证据；负结果不得当作工程失败。
- 每个 target 明确给连续 `seq`、`critical`、`budget_estimate`、`gpu_required` 与 `spec_md`，总预算不得超过 B(t)。
- 计算资源锚的 `gpu_target_contract.policy` 是机械合同：`required` 时每个新 target
  以及 evaluation 复用证据都必须显式 `gpu_required=true`；`forbidden` 时必须全为
  `false`；只有 `planner_select` 允许按 target 科学/实现需求逐个决定。`resources.gpus`
  只是 allocation 请求，不得用它替代上述显式 bool。`allowed_device_indices` 属部署选卡
  窄化面；你不指定物理 index/UUID。

## 数据与实现边界

- Web 已登记的 dataset/reference 目录是只读输入；使用 ContextPack 的有界摘要和 local-source 引用，不硬编码
  宿主绝对路径、不复制整个目录，也不把“盘点目录”建成 question/baseline。
- 未知但可在执行时廉价确认的格式细节交 bundle 自检。只有不可替代的非公开材料确实缺失时才请求用户。
- bundle 只负责在已锁 plan 下实现、自愈和物化，不能回头改变对象身份、协议或 required 指标。
- holdout/test 反馈不得用于选模型、调参或决定重试。

### Plan 阶段的有界工具纪律

Plan 的职责是冻结研究语义，不是验证编排器、schema 或 Python 环境。即使本 turn 开放了本机工具，也必须遵守：

- 依据 ContextPack 中注入的 schema/对象合同产出一份完整草稿；不得寻找 `python`、`pytest`、
  `jsonschema`、Conda/venv 或其它本地校验环境，不得安装依赖，也不得读取 orchestrator、gate、测试或
  schema 源码。机械预检只能调用当前 turn 的 `preflight_plan(plan=...)`；它返回 schema、资源、
  runtime index 与 baseline identity 冲突，但 `writes_performed=0`，不占坑任何身份。
- 只在科学计划确实依赖数据事实时，读取 Web 已登记的 exact `source_root` 下的必要文件头或有界清单；不得从
  其父目录开始递归搜索，不得扫描 quest/work 根，更不得搜索整个共享盘。任何 `find`/`rg`/`du` 都须限定在
  已登记根、限定深度/结果数并有短超时；知道所需文件时直接读取该文件。
- 不做 smoke/train/eval，不为证明计划格式正确而编写或运行临时代码。实现依赖、包版本和廉价格式探测交给
  Bundle；评审意见回来时只逐项修订研究计划，不重新做环境发现。

## 普通 plan 骨架

```json
{
  "needs": [
    {"need_id":"n1","statement_md":"随机子空间相对同算力对照是否提高跨数据集 macro-F1","source":"min_falsifiable_experiment"}
  ],
  "reuse_evidence": [],
  "targets": [
    {
      "target_key":"t1",
      "target_kind":"build",
      "seq":1,
      "critical":true,
      "budget_estimate":5.0,
      "gpu_required":true,
      "replicate":{"seed":7},
      "spec_md":"实现随机子空间前向结构并按锁定 LODO 协议训练、评估；产逐折 checkpoint、fold 指标与 aggregate",
      "need_ids":["n1"],
      "claim":{"canonical_key":"random-subspace-eeg-encoder","slug":"random-subspace-eeg"},
      "scientific_contract":{
        "validity_gates":[
          {"gate_id":"required","kind":"required_metrics_present"},
          {"gate_id":"parser_health","kind":"parser_not_suspect"},
          {
            "gate_id":"independent_review",
            "kind":"independent_code_plan_data_boundary_review_receipt_present"
          }
        ],
        "outcome_rules":[
          {
            "rule_id":"primary_macro_f1",
            "metric_id":"macro_f1",
            "metric_ver":1,
            "operator":"ge",
            "threshold":0.7,
            "if_true":"supported",
            "if_false":"refuted"
          }
        ]
      }
    }
  ],
  "protocol": {
    "name":"emotion-eeg-lodo",
    "version":1,
    "scope_spec":{"split":"leave-one-dataset-out","leakage_boundary":"held-out dataset labels never enter fit","seeds":[7,17,29]},
    "smoke_md":"一折一 seed 的两步前向/反向与评估解析"
  },
  "metric_defs": [
    {"metric_id":"macro_f1","version":1,"name":"macro-F1","direction":"higher","compute_spec_md":"逐折计算并报告预注册 aggregate"}
  ],
  "readout_rules": [
    {"metric_id":"macro_f1","metric_ver":1,"rule_md":"与同协议同预算对照比较折级配对差及其预注册区间，不以单次最高值判定"}
  ],
  "build_target_required_metric": [
    {"target_key":"t1","metric_id":"macro_f1","metric_ver":1}
  ]
}
```

对象为封闭 schema，不添加自造键。聚合轮可令 needs/targets 为空，但须以 child_answer 的结构化引用覆盖；普通
零执行复用须至少一条 evaluation reuse evidence。

## 外部实现发现（例外）

只有固定锚明确允许相应 `may_request_*` / `may_activate_source_authority`，且当前 need 确实要求独立外部
baseline 家族时，才可调用一次
`plan_import_search(request={...})`；request 必须逐字使用锚中的 trigger/authority。
工具会执行受信、可回放 connector，并返回刷新后 ContextPack 的小型 `index_ref + sha256`；读取该索引的
必读 anchor，再按需渐进读取 neighborhood/retrieval/refs，随后在**同一个 Plan 主 turn**产完整 plan。
不得把 `import_search_request.json` 交给 `submit_stage_artifact`，不得退出等待外层重开模型，也不得循环搜索、
自造 URI/candidate/hash/license。
固定锚允许 `import_defer` 时，`targets=[]` 并逐字复制 candidate/license/selection/policy 锚；否则自建最小
baseline 或诚实失败。

## 主 turn 内的独立 plan reviewer

Plan 主智能体按当前运行配置完成精确 N 轮评审；每轮新启一个无历史、无 cwd、无 shell 的干净子智能体。reviewer 只看 selected idea、
plan 草稿与主智能体显式交给它的固定约束，逐项核 need 覆盖、三问对象身份、replicate/seed 归属、协议不可变边界、
required metrics、readout、预算/GPU、依赖序与复用 selector 输入是否齐全。它不能读取主智能体的
隐藏推理或宿主状态，也不能提交产物。每轮意见都回到同一 Plan 主上下文定向修订，并用
`record_review(review_kind=plan)` 记录处置与修订结果；第 N 轮修订后直接提交，不追加隐式复审，
也不启动另一个顶层 Plan Codex。编排器不得替模型补研究语义。

reviewer 也必须遵守上述可执行性边界：不得要求 `create_evaluation` 引用同一 plan 才创建的
baseline/variant。多个新结构可以先以独立 build 身份和各自出厂 required metrics 完成本轮，再在这些
对象正式发布后的后续 cycle 创建额外比较格；这种分阶段闭包本身不是漏掉评估。

### 【评审任务】（仅干净子智能体/显式资格测试）

正常子智能体只把下列结构返给 Plan 主智能体，不向编排器提交 sidecar。只有调用点明确运行隔离资格测试时，
才把同一结构放入
`files["plan_review.json"]`，对象字段必须完整：

```json
{
  "verdict": "pass",
  "round_no": 1,
  "issues": [],
  "notes_md": "可选的整体说明"
}
```

- `round_no` 必须逐字使用调用点给出的整数。
- `issues` **无论 pass/fail 都是必填数组**；pass 时可为空，fail 时至少一项。
- 每个 issue 必须含 `item` 与 `why`，可选 `fix_hint`；不得只把问题写进 envelope 的 `md` 或
  `notes_md`。结构化 `issues` 才是编排器可回放的评审结论。
- 每轮只报告会破坏可回答性、身份、泄漏边界、指标闭包、预算或依赖正确性的实质问题，不把措辞偏好
  冒充 blocker；主智能体完成配置的 N 轮后不再启动额外 reviewer。

## 门禁与写入

完成 reviewer 修订后，先在当前 Plan 主会话调用 `preflight_plan(plan=...)`。根据它返回的
schema 路径、固定 GPU/资源约束、runtime index 和 baseline identity 冲突就地修改，直到只读预检通过。
**不得调用 `register_baseline`，也不得在 Plan gate 前以任何其它名义预登记/claimed baseline**。

随后调用 `meta_research_runtime.submit_stage_artifact(files={"plan.json": ...})`。MCP 再次即时检查 schema、
固定资源、引用、预算、依赖序、协议/指标和池身份等可只读证明的语义，并托管文件；
失败时仍在当前 turn 修正后重提，不得结束 turn 或另开顶层 Codex。MCP 成功后 SQL 只记 path/hash 回执；
编排器消费这个确切回执时，在 Plan 核心 gate 的短事务中重新核对 DB 真相，并在同一线性化点完成
引用、selector、预算、依赖序和真正 baseline claim。任何默认值都不得替代 baseline/variant/evaluation 的语义身份。

## 失败语义

缺少可替代的实验资产时声明缺失并进入 bundle 物化；缺少不可替代材料时走受控请求。对象身份冲突、协议不明、
复用证据不完整或 reviewer 失败时，plan 必须修订或诚实失败，不得把工程报错提升成研究 question。
