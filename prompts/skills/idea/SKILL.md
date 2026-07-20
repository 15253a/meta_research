# SKILL · idea —— 已选问题下的候选假设/方案（常驻主智能体 + WildIdea MCP + 干净子评审）

> 版本：wildidea-adapter-v1。按《第一部分》§3.2 与流程图 03-Idea 实现；产物 schema =
> `schemas/idea_set.schema.json`。发散引擎固定为
> `wildidea@6ff66ada15b0047b2e03d229f2e9543c542df598`，仅经
> `meta-research-wildidea-adapter-v1` 读取已 vendored 的上游规则与锚点。
> 正常路径只有一次 Idea 顶层 runner_call：主智能体在同一个连续 turn 内判 NEED、调用
> `wildidea_expand`，按需调用 `wildidea_search`，生成候选，再启动恰好一个干净子智能体评审。
> 主智能体吸收意见后直接提交最终 `idea_set.json`。旧的顶层“生成会话 → 盲审会话 → 外部合并”
> 仅是显式隔离资格测试的兼容路径，不得在普通研究运行中使用。

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
  `need_innovation` 后必须在当前 Idea turn 调用 `wildidea_expand(need_innovation=...)`；返回的 pinned
  槽位、seed、阈值和 engine 身份是数据能力，不会启动第二个顶层 Codex。
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
3. **发散**：按 MCP 返回的 9 个 pinned 锚点依次做 `problem_card → source-first →
   claimed_method → systematicity → repair → reangle → batch diversity`：
   - `problem_card` 只描述 actor / constraint / bottleneck_relation / desired_change / trade_off，不含方案；
   - 先冻结源域 `source_phenomenon` 的输入/状态/输出/反馈，再写目标域方案；原型不得含任务禁词；
   - 只有真实具名方法才写 `claimed_method`，否则明记“抽象概括（无真实具名对应）”；
   - 用同一因果/功能关系串联多个源→目标映射，写入 `audit_mapping.shared_relations`；只有表面相似应淘汰；
   - 每槽最多 3 次：初次失败后定向 **repair**，同类失败再现则改结构角度 **reangle**，不无限盲抽；
   - 完成 **batch diversity** 后交付恰好 top 3，三者不得是同一方案换名。
4. 每个候选的 `min_falsifiable_experiment` 都必须含对照与明确失败判据。用候选的英文查询调用
   `wildidea_search`，只使用工具实际返回的结果更新 `nearest_neighbor_diff`、
   `strongest_rebuttal`、`novelty_refs` 和 `novelty_status`。
5. **干净子评审**：启动恰好一个无历史的子智能体，只把问题、每候选 `candidate_id +
   audit_mapping` 四字段、评分阈值和必要的冻结检索摘要交给它。不给生成 transcript、隐藏推理或工作目录。
   评审者对每候选独立给 0–10 的 Structural Depth / Domain Distance / Applicability / Novelty /
   Unexpectedness / Non-Obviousness 与中文 rationale；Structural Depth 必须核 systematicity。它只返回
   findings，不产生可提交 sidecar。主智能体检查评审映射完整性，然后调用
   `record_review(review_kind=idea)` 记录评审。
6. **机械选择**：WildIdea 用 SD≥6、DD≥7、AP≥6、NV≥8；bypass 仅以 SD≥6 为门槛。在过线者中
   按六维均分最高、同分 `candidate_id` 字典序确定 `selected_id`；全部候选不合格则为 null，
   不得因“流程需要一个想法”放水。
7. **提交**：主智能体直接组装最终 `files={"idea_set.json": ...}` 并调用
   `meta_research_runtime.submit_stage_artifact`。工具返回的 schema/身份错误必须在当前 turn 修正后重提；
   不结束 turn，不启动新顶层 Codex。`provenance` 是可选机械身份：没有服务端精确回执时整体省略，
   绝不猜 engine/hash/model/sampling。

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
