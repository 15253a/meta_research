# SKILL · idea —— 已选问题下的候选假设/方案（常驻主智能体 + 路径内评审）

> 版本：wildidea-adapter-v1。按《第一部分》§3.2 与流程图 03-Idea 实现；产物 schema =
> `schemas/idea_set.schema.json`。发散引擎固定为
> `wildidea@6ff66ada15b0047b2e03d229f2e9543c542df598`，仅经
> `meta-research-wildidea-adapter-v1` 读取已 vendored 的上游规则与锚点。
> 正常路径只有一次 Idea 顶层 runner_call：主智能体在同一个连续 turn 内判 NEED 并调用
> `wildidea_expand`。服务端由此冻结 `generation_path`：WildIdea 路径在 capability 内部完成
> generation 并返回服务端生成的 exact draft，再由 `wildidea_audit` 完成独立盲审与机械 merge，
> 主智能体只吸收其结果；bypass
> 路径才按运行时 review 轮数启动干净子智能体并就地修订。旧的两个顶层 Idea 会话仅是显式隔离
> 资格测试的兼容路径，不得在普通研究运行中使用。

## 通用

- **触发条件**：攻坚轮（route=attack 起手）进入 idea 阶段。
- **对象边界**：输入 Qn 必须是 reasoning/tree_ops 已经准入并入库的 research question；idea 只是挂在
  该 Qn 下的候选研究路径。你不得创建、改写、分解或替换 question，不得输出 tree_ops，也不得把
  candidate_id 当 question_id。
- **读取**（context_pack 四区，§3.1.1 idea 行）：选中问题卡 Qn + 祖先链 · 该问题**全部已试
  idea 及结局**（防重复造轮）· 相关已关闭结论（带 applicability 徽标）· 相似失败卡 · 文献检索区。
- **门禁与写入**：产物经 gate 落 IDEA + DECISION；**全部候选及结局（含 failed/bypass）入账**。
- **失败语义**：全部候选不合格 → 阶段失败 = 研究轮正常收尾（cycle=done、Qn 置 inconclusive、
  visit+1），下一轮 reasoning 裁决换 idea / 降级 / 分解。**不是系统失败，不得伪造合格候选。**
- **工程隔离**：目录/文件/资产盘点、修代码、处理报错、环境/依赖/权限配置、部署恢复不是 idea，
  也不能作为 bypass 候选。若上下文中的 Qn 本身是这类任务，这是上游 question contract 违规；不得为它
  生成或包装“研究方案”，只在 md 如实报告违规，交由编排器失败收口。

## WildIdea 适配边界

- 采用 policy 固定的 `problem_type=research`、`engine.profile=research`、`slot_count=9`、
  `candidate_top_k=3`、`max_attempts=3`。research 门槛为 Structural Depth≥6、Domain Distance≥7、
  Applicability≥6、Novelty≥8；生成侧门槛只用于自筛，最终 pass/fail 仍归独立判官。
- 保留上游 `problem_card → source-first → claimed_method → systematicity → repair → reangle →
  batch diversity` 的机制顺序；适配器只把它投影到本系统的封闭 JSON，不重写为另一套创新算法。
- 上游的海报/HTML 是展示层，不是本系统契约：**严禁生成 HTML**、search sidecar 或输出路径；不得执行
  `search_helper.py`、`search_char.py` 或其它上游脚本，也不得调用另一个模型来代替 WildIdea。判定
  `need_innovation` 后必须在当前 Idea turn 调用 `wildidea_expand(need_innovation=...)`；WildIdea 分支
  会在 capability 内运行 pinned generator 并返回 owner-bound draft，不会替换常驻主智能体或启动第二个
  顶层 Codex。
- 先为候选准备 5–512 bytes 的英文普通文本查询，再按需在当前 turn 调用 `wildidea_search(queries=...)`。
  该 Idea-only 工具若禁用检索，会显式返回 `enabled=false`，此时 `novelty_refs=[]`且
  `novelty_status=联网查重未启用·文献级待验证`。若它返回冻结内容寻址回执，才可抄入返回的
  refs 并使用「联网粗查已启用·文献级待人工验证」。URL、API 语法、回执和 hash 不得自造。
- 内置 live Web 可辅助生成，但它不是 P6 快照；模型不得伪造内容 hash。受控 API 零命中也不等于查重通过，
  文献级结论仍需人工复核。

## 【Idea 主任务】（一个连续 turn）

1. **判 NEED**：读问题卡与已试 idea，先回答“这个问题需要全新创新吗？”。
   已有成熟研究方法可直接检验 Qn 时令 `need_innovation=false`走**旁路**；已试想法反复失败或
   问题确需新机制时令 `need_innovation=true`走 WildIdea **发散**。判定后立即调用
   `wildidea_expand`，不得自造槽位、seed 或阈值。
2. **旁路**：给恰好一条现成研究路径（`generation_path=bypass`），补齐 `audit_mapping` 四字段及全部
   common 字段，不携带 `wildidea_extra`。DD/Novelty 可诚实偏低，不得为过“创新分”伪装跨域来源。
3. **发散**：`wildidea_expand` 的内部 generator 会按 9 个 pinned 锚点依次做 `problem_card →
   source-first → claimed_method → systematicity → repair → reangle → batch diversity`：
   - `problem_card` 只描述 actor / constraint / bottleneck_relation / desired_change / trade_off，不含方案；
   - 先冻结源域 `source_phenomenon` 的输入/状态/输出/反馈，再写目标域方案；原型不得含任务禁词；
   - 只有真实具名方法才写 `claimed_method`，否则明记“抽象概括（无真实具名对应）”；
   - 用同一因果/功能关系串联多个源→目标映射，写入 `audit_mapping.shared_relations`；只有表面相似应淘汰；
   - 每槽最多 3 次：初次失败后定向 **repair**，同类失败再现则改结构角度 **reangle**，不无限盲抽；
   - 完成 **batch diversity** 后交付恰好 top 3，三者不得是同一方案换名。
   主智能体不得重新生成、补写或修订该服务端内部生成的 exact draft。
4. 每个内部生成候选的 `min_falsifiable_experiment` 都必须含对照与明确失败判据。受控 novelty
   backend 会按 draft 中的英文查询冻结检索依据；`wildidea_search` 仅可辅助 NEED/bypass 判断，
   不得用来改写已经冻结的 WildIdea draft，也不得自造证据 hash。
5. **按路径评审**：
   - `generation_path=wildidea`：把 `wildidea_expand` 返回且不含
     `audit_scores/selected_id/provenance` 的 exact draft 原样交给
     `wildidea_audit`。该工具以 question-only + `candidate_id/audit_mapping` 投影运行 WildIdea
     自己的独立盲审，再按 SD≥6、DD≥7、AP≥6、NV≥8、六维均分及 candidate_id 字典序机械 merge。
     主智能体必须吸收并原样使用工具返回的 `idea_set`；这条路径不得再 spawn native child，也不得
     自行改 audit 分数或选择。
   - `generation_path=bypass`：按当前运行配置完成精确 N 轮 native child review；每轮新启一个
     `fork_turns=none` 的无历史子智能体，通过 `prepare_review/read_review_input` 读取 owner-bound
     候选，只返回反驳性 findings。主智能体逐条 disposition、修订并调用 `record_review`；完成一轮
     修改即计一轮，第 N 轮后直接提交，不追加隐式第 N+1 次复审。bypass 最终仅以 SD≥6 为门槛。
6. **选择诚实性**：全部候选不合格时 `selected_id=null`，不得因“流程需要一个想法”放水。服务端
   会校验提交的 `need_innovation`、每项 `generation_path`、内部 audit/native review 回执及最终 hash，
   不能靠 caller 自报切换路径。
7. **提交**：主智能体直接组装最终 `files={"idea_set.json": ...}` 并调用
   `meta_research_runtime.submit_stage_artifact`。工具返回的 schema/身份错误必须在当前 turn 修正后重提；
   不结束 turn，不启动新顶层 Codex。WildIdea 路径必须提交 `wildidea_audit` 返回的 exact
   `idea_set`；`provenance` 是可选机械身份，没有完整 provider 回执时整体省略，绝不猜 engine/hash/model/sampling。

## 最终产物骨架

`idea_set.json` 是封闭对象；候选不得加 title/problem_card/claimed_method/source_phenomenon/HTML 字段。
`wildidea_extra` 仅 `generation_path=wildidea` 时携带，bypass 必须整体省略。

```json
{
  "need_innovation": true,
  "candidates": [{
    "candidate_id": "c1",
    "generation_path": "wildidea",
    "audit_mapping": {
      "source_domain": "<>", "target_domain": "<>",
      "object_mapping": "<>", "shared_relations": "<>"
    },
    "core_claim": "<>",
    "mechanism": "<claimed_method + 目标域操作>",
    "assumptions": ["<>"],
    "min_falsifiable_experiment": "<对照 + 失败判据>",
    "novelty_type": "<评估协议|训练目标|表征结构|推理机制|部署策略|数据准入|采样|校准|路由|损失>",
    "novelty_status": "联网查重未启用·文献级待验证",
    "wildidea_extra": {
      "source_isof": "<>", "source_prototype": "P01 <>",
      "deanchor_level": "<不成立|部分成立|成立>", "degenerate_form": "<>",
      "nearest_neighbor_diff": "<>", "strongest_rebuttal": "<>"
    }
  }],
  "audit_scores": [{
    "candidate_id": "c1",
    "scores": {
      "structural_depth": 0, "domain_distance": 0, "applicability": 0,
      "novelty": 0, "unexpectedness": 0, "non_obviousness": 0
    },
    "decision": "<pass|fail>", "rationale": "<中文理由>"
  }],
  "selected_id": "<candidate_id 或 null>",
  "novelty_refs": []
}
```
