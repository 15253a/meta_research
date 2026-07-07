# SKILL · idea —— 出题/假设生成（NEED 分支 + 统一独立审计）

> 版本：m0-1。按《第一部分》§3.2 与流程图 03-Idea 实现；产物 schema = `schemas/idea_set.schema.json`。
> 本阶段共 **两次独立 runner_call**：①生成（phase=idea，本文件【生成任务】节）②判官（phase=audit，
> 本文件【判官任务】节）。两次调用不同会话、判官看不到生成过程（§3.1.3 审计契约）。
> M0 骨架说明：vendored wildidea 引擎与联网查重尚未接入（`idea_engine_adapter` 骨架期）——
> "需要发散"路径由生成会话按 wildidea pipeline 步骤**在会话内**执行；`novelty_refs` 允许为空数组，
> `novelty_status` 恒为固定诚实文本。M1+ 接入真引擎后本文件只换执行方式、不换契约。

## 通用

- **触发条件**：攻坚轮（route=attack 起手）进入 idea 阶段。
- **读取**（context_pack 四区，§3.1.1 idea 行）：选中问题卡 Qn + 祖先链 · 该问题**全部已试
  idea 及结局**（防重复造轮）· 相关已关闭结论（带 applicability 徽标）· 相似失败卡 · 文献检索区。
- **门禁与写入**：产物经 gate 落 IDEA + DECISION；**全部候选及结局（含 failed/bypass）入账**。
- **失败语义**：全部候选不合格 → 阶段失败 = 研究轮正常收尾（cycle=done、Qn 置 inconclusive、
  visit+1），下一轮 reasoning 裁决换 idea / 降级 / 分解。**不是系统失败，不得伪造合格候选。**

## 【生成任务】（第 1 次调用，phase=idea）

产出 `files["idea_set.draft.json"]`（schema = `schemas/idea_set_draft.schema.json`：
= idea_set 去掉 `audit_scores`/`selected_id`——此两项由判官调用产出，编排器合并成完整
idea_set.json 后按 `schemas/idea_set.schema.json` 终校）。步骤：

1. **判 NEED**：读问题卡与已试 idea，先回答"这个问题需要全新创新吗？"
   - 已有成熟做法可直接验证、或问题本身是工程/对照性质 → `need_innovation=false`，走**旁路**；
   - 已试想法反复失败、或问题要求新机制 → `need_innovation=true`，走**发散**。
2. **旁路**（无需重大创新）：直接给**单一**现成想法（generation_path=bypass，不跑发散），
   仍须补齐 `audit_mapping`（源域 = 现成方案来源/目标域基线/近邻先例）与全部公共字段
   （novelty_status 同样逐字填固定文本，见下 d）；
   **不携带 `wildidea_extra`**。DD/novelty 低是诚实的，不影响入选。
3. **发散**（wildidea 跨域机制迁移，§3.2.3 pipeline，会话内执行；候选标
   `generation_path=wildidea` 并携带 `wildidea_extra`——下列 a–d 步对应其 6 个必填键：
   a→`source_isof`+`source_prototype`，c→`deanchor_level`+`degenerate_form`，
   d→`nearest_neighbor_diff`+`strongest_rebuttal`）：
   a. **源域冻结**：抽 1 个非本领域机制（输入/状态/输出/反馈 + 触发条件/阈值/边界），编号 P01…；
      冻结描述**禁出现**问题任务词（数据集名/指标名/常规方法名——禁词来自问题卡任务词）。
   b. **映射**：源域原型 → 用户领域怎么干（拿什么数据、什么顺序、什么条件触发、触发后改什么）。
   c. **去锚点审查**：写退化物与成立等级（不成立/部分成立/成立）；退化后只剩命名差异 → 淘汰重抽。
   d. **查重（M0 降级）**：无联网工具，凭上下文与常识写最近邻同异 + 被吃掉风险 + 最强反驳；
      `novelty_refs=[]`；novelty_status **逐字**填固定文本「联网粗查已启用·文献级待人工验证」
      （schema const 校验，多一字少一字都会被拒；绝不写"查重通过"）。
   e. **自评筛选前 3**（policy idea.candidate_top_k）：这属生产侧自筛、不是判官，不产 audit_scores。
4. 每个候选给 `min_falsifiable_experiment`（带对照与失败判据）——它只是 plan 的输入素材。
5. `provenance` 填已知项（prompt_hash/model 由编排器注入 context_pack 提供，照抄）。

**输出骨架（键名逐字，封闭对象——多一键即被拒；`<>` 为占位）**：

```json
{ "need_innovation": <true|false>,
  "candidates": [
    { "candidate_id": "c1",
      "generation_path": "<wildidea|bypass>",
      "audit_mapping": { "source_domain": "<>", "target_domain": "<>",
                         "object_mapping": "<>", "shared_relations": "<>" },
      "core_claim": "<>", "mechanism": "<>",
      "assumptions": ["<>"],
      "min_falsifiable_experiment": "<>",
      "novelty_type": "<十选一：评估协议/训练目标/表征结构/推理机制/部署策略/数据准入/采样/校准/路由/损失>",
      "novelty_status": "联网粗查已启用·文献级待人工验证",
      "wildidea_extra": { "source_isof": "<>", "source_prototype": "<>",
                          "deanchor_level": "<不成立|部分成立|成立>", "degenerate_form": "<>",
                          "nearest_neighbor_diff": "<>", "strongest_rebuttal": "<>" } } ],
  "novelty_refs": [],
  "provenance": { "model": "<照抄注入值>" } }
```

骨架纪律：顶层只许这四个键；候选内除上列键外**一律不加**（无 title/phase/cycle/question_id）；
`wildidea_extra` **仅** generation_path=wildidea 时携带、bypass 必须整体省略；
最近邻/最强反驳属 `wildidea_extra` 内、不放候选顶层。

## 【判官任务】（第 2 次调用，phase=audit，独立会话）

输入 = **映射包**（编排器抽取，严格对齐 §3.1.3 的穷举清单：用户问题 + 每候选的
candidate_id（仅作评分回指键）+ audit_mapping 四字段——源域 / 目标域 / 对象映射 / 共享关系；
**不含 core_claim / novelty_type / wildidea_extra / 任何生成过程痕迹**——判官凭映射盲评 6 维）。
产出 `files["idea_audit.json"]`：`{ "audit_scores": [...], "selected_id": "..."|null }`
（字段定义同 idea_set.schema.json 对应项）。规则：

1. 逐候选 6 维 0–10 打分：Structural Depth / Domain Distance / Applicability / Novelty /
   Unexpectedness / Non-Obviousness；每条给中文 rationale。
2. **SD < 阈值（context_pack 注入 policy idea.sd_threshold，默认 6）即 decision=fail**（淘汰）。
   统一门口径：bypass 用同一 6 维，判"是否适合当前 NEED"；**低 DD/novelty 不作
   need_innovation=false 路径的淘汰理由**，不强制奖励新奇度。
3. `selected_id` = 过线者中**审计总分最高**者（同分按 candidate_id 字典序，确定性 tie-break）；
   全不过线 → null。
4. 你是独立判官：不得向生成侧妥协，不得因"流程需要一个想法"而放水。

**输出骨架（键名逐字）**：

```json
{ "audit_scores": [
    { "candidate_id": "<对应候选>",
      "scores": { "structural_depth": <0-10>, "domain_distance": <0-10>,
                  "applicability": <0-10>, "novelty": <0-10>,
                  "unexpectedness": <0-10>, "non_obviousness": <0-10> },
      "decision": "<pass|fail>", "rationale": "<中文理由>" } ],
  "selected_id": "<candidate_id 或 null>" }
```
