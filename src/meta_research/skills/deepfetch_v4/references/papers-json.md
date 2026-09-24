# `papers.json` 台账契约

创建或修改公开台账前读取本文件；它定义 `deepfetch.papers.v4` 的内容与分工。

## 顶层

恰含以下键：

```json
{
  "schema_version": "deepfetch.papers.v4",
  "topic": {
    "input": "原始任务",
    "interpretation": "简洁检索解释",
    "search_concepts": [],
    "scope_notes": []
  },
  "run": {
    "intensity": "medium",
    "active_search_budget_minutes": 13,
    "active_search_elapsed_seconds": 0,
    "dimensions_used": [],
    "stopping_reason": null
  },
  "paper_order": [],
  "papers": {},
  "missing_fulltexts": [],
  "limitations": []
}
```

`paper_order` 恰好包含 `papers` 的每个 key 一次。`missing_fulltexts` 按此顺序列出全部 `fulltext_path=null` 的论文。最终 `dimensions_used` 包含 `text_queries`、`literature_roles`、`citation_graph`；它们是语义维度，OpenAlex 和 Web 均可参与，引文方向和深度由 Agent 选择。`limitations` 记录简短运行级证据／覆盖限制。未知标量为 `null`，未知、空或不适用集合为 `[]`。

## 论文身份与元数据

每个 `papers[paper_id]` 恰含 `identity`、`metadata`、`pre_understanding`、`fulltext_path`、`reading`。

创建时用最强已核实标识形成稳定 `paper_id`：依次优先 DOI、arXiv、OpenAlex、确定性标题指纹。后来取得更强标识也保留原 ID；合并需明确身份依据。DOI 不带 URL 前缀，OpenAlex token 为 `W...`，标题保持已核实原文。OpenAlex 记录不是必要条件。

`metadata` 只保存书目事实。`authors`、`institutions` 为名称数组；`citation_count_observed_at` 与 `cited_by_count` 配对，使用 RFC 3339 时间，缺时间的计数视为未知。`source_urls` 是元数据位置，不证明已取得全文。

## 预理解

主智能体填写：

- `summary`：仅由所列依据支持的谨慎概述。
- `evidence_level`：`title_only | citation_context | abstract_supported`。
- `basis`：`{"type":"title|citation_context|abstract|metadata","source":"...","locator":null}` 条目。
- `why_included`：与本任务的关联及保留理由。
- `uncertainty`：尚未核实之处。

较弱更新不能替换较强摘要或不确定性记录。最终校验要求摘要、依据、纳入理由及与证据级别匹配的 basis；`abstract_supported` 还需实际存储摘要。

## 全文

`fulltext_path` 为 `null`，或 `fulltext/` 下以 `.pdf`、`.html`、`.xml` 结尾的相对路径。hash 与获取诊断放私有状态；路径只证明已登记核实的本地正文。最多 10 篇可有非空路径，不限制元数据占位条目。

## 阅读

阅读前严格使用以下空结构：

```json
{
  "status": "not_read",
  "understanding_summary": null,
  "methods": [],
  "experimental_setup": {
    "datasets_samples": [],
    "protocols": [],
    "baselines_controls": [],
    "metrics": [],
    "hardware_software": []
  },
  "key_claims": [],
  "limitations": [],
  "artifacts": {
    "code": {"reported": null, "items": []},
    "data": {"reported": null, "items": []},
    "model": {"reported": null, "items": []},
    "project": {"reported": null, "items": []},
    "supplement": {"reported": null, "items": []}
  },
  "credibility": {
    "score": null,
    "assessment_confidence": null,
    "rationale": null,
    "strengths": [],
    "concerns": []
  },
  "evidence_locators": [],
  "notes": []
}
```

成功 Reader 设置 `status=complete`，填写全文支持的内容，`understanding_summary` 必填；未涉及或未报告的项目保留空数组。`experimental_setup` 记录数据／样本、协议／划分、基准／对照、指标及报告的硬件软件。

每个关键主张：

```json
{
  "claim": "论文实际提出的主张",
  "evidence_locators": ["loc-1"],
  "internal_support": "supported",
  "support_rationale": "论文自身证据如何支持或限制该主张"
}
```

`internal_support` 为 `supported | partially_supported | unsupported | unclear`，判断论文内部支持，不宣告外部真理。局限条目为 `{"description":"...","source":"authors|reader","evidence_locators":[]}`。

各材料类别采用 `{"reported":true|false|null,"items":[]}`，条目为 `{"name":null,"url":null,"evidence_locators":[]}`。Reader 记录论文是否报告材料，不替链接证明当前可访问性。

定位条目为 `{"id":"loc-1","page":null,"section":null,"element":null,"description":"..."}`，主张引用同一阅读对象内的 ID；无页码时用章节或元素。

`credibility.score` 是 1–5 的整数，无可辩护判断时为 `null`：1 表示核心推断存在严重方法错配或缺乏支持；2 表示重大设计威胁；3 表示可用但有重要不确定性；4 表示主要结论有强证据；5 表示证据异常稳健、透明且内部一致。内部证据优先，期刊、机构、作者、引用和年代只是弱背景。`assessment_confidence` 为 `low | medium | high | null`，表达对该评价的信心。

终态 Reader 失败使用相同空结构，设 `status=failed` 并加一条简短失败说明，不推断理解、主张、材料或评分；成功重试可替换失败记录。

## 分工与最终不变量

主智能体拥有任务、运行、排序、身份、元数据、预理解、全文选择和运行限制；Acquisition 返回文件和简短状态，确定性工具登记路径；Reader patch 只拥有所分配论文的 `reading`，错文隔离及清路径由合并工具完成。

运行内最多 10 个不同 `paper_id` 首次分配 Reader，同文重试不新增，首次分配后的失败仍占名额。无全文论文终态为 `not_read`，有路径论文终态为 `complete` 或 `failed`。元数据预理解可贡献文献图景，不能建立全文实验细节、材料事实、内部支持或可信度。
