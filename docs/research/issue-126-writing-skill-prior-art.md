# Issue #126：Writing paper／PPT Skill 一手来源与可迁移设计

调研日期：2026-08-25

适用范围：[`#126 Writing paper、PPT 与外部交付`](https://github.com/15253a/meta_research/issues/126)，并以其绑定的 [`#125 report Writing 核心闭环`](https://github.com/15253a/meta_research/issues/125) 为兼容基线。

## 结论先行

没有一个现成 Skill 或工具同时满足 #126 的完整生产合同。可取的组合是：

1. 用 STORM 的“预写作／提纲／分节写作”分层来组织长文流程，但不采用它的实时搜索与 Wikipedia 文体；
2. 用 PaperQA2 的“先收集证据、再从受限 citation key 集合生成内容、证据不足时明确拒答”约束起草；
3. 用 GitHub `build-evidence-map` Skill 的 claim／evidence／unknown 与有类型边，形成可审计的内容—证据中间表示；
4. 用 OpenAI Google Slides Skill 的“先内容结构、再 slide archetype、先做代表性子集、逐页 thumbnail 回读”作为 PPT 工作流；
5. 用 Anthropic PPTX／DOCX Skill 观察到的 content／package／visual 三层 QA，但因许可证限制只能独立实现同类能力，不能复制其 Skill 文本、代码或资产；
6. 用 Quarto／PptxGenJS 一类程序化 renderer 作为可替换 Adapter，而不是新的 Writing identity 或 State Owner；renderer 必须固定工具链与全部输入，并把 OOXML 时间戳、ZIP 元数据、稳定 ID 等非确定因素规范化后再封存最终字节；
7. publish／send／submit 永远在 renderer 之后走 #126 已规定的 HC confirmation 与 AR execution／reconciliation seam，不能混进 paper/PPT authoring Skill。

因此，建议把 Writing Skill 的核心产物定义为同一 `AssetVersion` lineage 上的“可审计语义稿件 IR + claim/evidence ledger”，再由类型 Adapter 生成 paper 或 PPT。文件扩展名、模板和外部 Provider 都不应反过来成为产品领域模型。

## 调研方法与许可证口径

- 只采用项目官方文档、官方源码仓库、仓库内 Skill、论文团队源码等一手来源；所有源码结论固定到完整 commit，而不是漂移的 `main` 链接。
- “可迁移”分成两层：可以独立实现的工作流思想；可以依法复用的代码／资产。前者不等于后者。
- 下表是仓库在指定 commit 的声明，不构成法律意见。若仓库或相关目录没有许可证，按 GitHub 官方说明，无许可证时默认版权法适用，不能据“公开可见”推定可复制、分发或制作衍生物（[GitHub Docs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)）。

## 固定来源清单

| 来源与固定版本 | 一手文件 | 许可证核对 | 本调研用途 |
|---|---|---|---|
| Stanford OVAL STORM `fb951af7744dab086e34962e9bc6fe878e145f83` | [`README.md`](https://github.com/stanford-oval/storm/blob/fb951af7744dab086e34962e9bc6fe878e145f83/README.md#L32-L52)、[`outline_generation.py`](https://github.com/stanford-oval/storm/blob/fb951af7744dab086e34962e9bc6fe878e145f83/knowledge_storm/storm_wiki/modules/outline_generation.py#L11-L72)、[`article_generation.py`](https://github.com/stanford-oval/storm/blob/fb951af7744dab086e34962e9bc6fe878e145f83/knowledge_storm/storm_wiki/modules/article_generation.py#L15-L50) | [MIT](https://github.com/stanford-oval/storm/blob/fb951af7744dab086e34962e9bc6fe878e145f83/LICENSE) | 长文预写作、提纲、分节生成的模块边界与局限 |
| Future-House PaperQA2 `57e89f7223b0960d5ee5ea048c69e3c47e088572` | [`README.md`](https://github.com/Future-House/paper-qa/blob/57e89f7223b0960d5ee5ea048c69e3c47e088572/README.md#L224-L242)、[`prompts.py`](https://github.com/Future-House/paper-qa/blob/57e89f7223b0960d5ee5ea048c69e3c47e088572/src/paperqa/prompts.py#L3-L68)、[`types.py`](https://github.com/Future-House/paper-qa/blob/57e89f7223b0960d5ee5ea048c69e3c47e088572/src/paperqa/types.py#L75-L94) | [Apache-2.0](https://github.com/Future-House/paper-qa/blob/57e89f7223b0960d5ee5ea048c69e3c47e088572/LICENSE) | 科学文献 evidence gathering、受限 citation key、内容 hash |
| GitHub `awesome-copilot` `build-evidence-map` `4742f265959bf025882314564b364d9d7af6e2d5` | [`SKILL.md`](https://github.com/github/awesome-copilot/blob/4742f265959bf025882314564b364d9d7af6e2d5/skills/build-evidence-map/SKILL.md#L16-L78)、[`validate.mjs`](https://github.com/github/awesome-copilot/blob/4742f265959bf025882314564b364d9d7af6e2d5/skills/build-evidence-map/scripts/validate.mjs#L1-L29) | [MIT](https://github.com/github/awesome-copilot/blob/4742f265959bf025882314564b364d9d7af6e2d5/LICENSE) | claim／evidence／unknown 图与 deterministic structural receipt |
| OpenAI `plugins` Google Slides `11c74d6ba24d3a6d48f54a194cd00ef3beea18f9` | [`slide planning`](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/google-drive/skills/google-slides/references/reference-slide-planning-and-layout-selection.md#L5-L73)、[`thumbnail verification`](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/google-drive/skills/google-slides/references/reference-thumbnail-visual-verification.md#L1-L90)、[`final pass`](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/google-drive/skills/google-slides/references/reference-new-deck-and-final-pass.md#L20-L37) | 该 commit 的仓库根和 `plugins/google-drive/` 子树未见许可证文件；仅观察，不复制 | PPT 内容规划、代表性子集、逐页视觉回读、结构与缩略图双重验证 |
| Anthropic `skills` PPTX／DOCX `3b3fad96af16a10759d930941b4520ba0c40edae` | [`pptx/SKILL.md`](https://github.com/anthropics/skills/blob/3b3fad96af16a10759d930941b4520ba0c40edae/skills/pptx/SKILL.md#L166-L223)、[`docx/SKILL.md`](https://github.com/anthropics/skills/blob/3b3fad96af16a10759d930941b4520ba0c40edae/skills/docx/SKILL.md#L35-L58) | [Proprietary；禁止复制、衍生与分发](https://github.com/anthropics/skills/blob/3b3fad96af16a10759d930941b4520ba0c40edae/skills/pptx/LICENSE.txt#L1-L30) | 观察成熟 Office artifact QA 工作流；不得迁入原文、脚本或资产 |
| Quarto CLI `d4cb49f1e70fb34e4cdf38edbb2f938c3ce7cc21`；Quarto 文档 `a6b7948a63daaef1fe9b2b5cf49b0144a6901528` | [`citations.qmd`](https://github.com/quarto-dev/quarto-web/blob/a6b7948a63daaef1fe9b2b5cf49b0144a6901528/docs/authoring/citations.qmd#L9-L44)、[`powerpoint.qmd`](https://github.com/quarto-dev/quarto-web/blob/a6b7948a63daaef1fe9b2b5cf49b0144a6901528/docs/presentations/powerpoint.qmd#L22-L91)、[`code-execution.qmd`](https://github.com/quarto-dev/quarto-web/blob/a6b7948a63daaef1fe9b2b5cf49b0144a6901528/docs/projects/code-execution.qmd#L21-L31) | Quarto CLI [MIT](https://github.com/quarto-dev/quarto-cli/blob/d4cb49f1e70fb34e4cdf38edbb2f938c3ce7cc21/COPYING.md)；其许可证页同时列出 Pandoc 为 GPLv2 依赖（[source](https://github.com/quarto-dev/quarto-web/blob/a6b7948a63daaef1fe9b2b5cf49b0144a6901528/license.qmd)） | 同一文本源的 citations、paper/PPT renderer、模板与 computation freeze 的真实边界 |
| PptxGenJS `3c9ec1b687c174952166f6a34b5e87ebf69fa469`（仓库版本 4.0.1） | [`README.md`](https://github.com/gitbrent/PptxGenJS/blob/3c9ec1b687c174952166f6a34b5e87ebf69fa469/README.md#L11-L45)、[`gen-xml.ts`](https://github.com/gitbrent/PptxGenJS/blob/3c9ec1b687c174952166f6a34b5e87ebf69fa469/src/gen-xml.ts#L1506-L1524)、[`gen-charts.ts`](https://github.com/gitbrent/PptxGenJS/blob/3c9ec1b687c174952166f6a34b5e87ebf69fa469/src/gen-charts.ts#L85-L97) | [MIT](https://github.com/gitbrent/PptxGenJS/blob/3c9ec1b687c174952166f6a34b5e87ebf69fa469/LICENSE) | 可编辑 OOXML PPTX renderer 候选及其非确定性时间戳风险 |
| Marp CLI `527edc3b30826cffe021ef05cc4812227a035ccc`（仓库版本 4.5.0） | [`README.md`](https://github.com/marp-team/marp-cli/blob/527edc3b30826cffe021ef05cc4812227a035ccc/README.md#L174-L205) | [MIT](https://github.com/marp-team/marp-cli/blob/527edc3b30826cffe021ef05cc4812227a035ccc/LICENSE) | Markdown-to-PPTX 的外观／可编辑性取舍，主要作为反例边界 |
| OpenAI `plugins` Hugging Face paper publisher，同为 `11c74d...` | [`SKILL.md`](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/hugging-face/skills/paper-publisher/SKILL.md#L177-L251)、[`arxiv.md`](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/hugging-face/skills/paper-publisher/templates/arxiv.md#L107-L178)、[`paper_manager.py`](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/hugging-face/skills/paper-publisher/scripts/paper_manager.py#L119-L177) | 相关仓库／子树未见许可证文件；仅作负面观察 | “格式模板假扮研究内容”和“authoring 与外部副作用混合”的反例 |

## 一手来源中的可迁移结构

### 1. Paper：把预写作、证据计划、起草和审查拆开

STORM 明确把长文生成分成“研究并形成引用／提纲”的预写作阶段，以及“依据提纲和引用生成全文”的写作阶段；源码进一步拆成 knowledge curation、outline、article generation、polish 四个模块（[`README.md` L44-L52、L273-L282](https://github.com/stanford-oval/storm/blob/fb951af7744dab086e34962e9bc6fe878e145f83/README.md#L44-L52)）。这说明提纲不是排版细节，而是一个在全文生成前可单独检查、可修订的 checkpoint。

可迁移到 #126 的部分：

- 先写“论文要回答什么、面向谁、采用何种论证／文类”，再写 section outline；
- 每个 section 先声明要支撑的 claim、允许使用的 evidence、图表与未解决缺口；
- 分节起草可以并行，但合并必须按稳定 section ID／原提纲顺序，而不是按 future 完成顺序；STORM 本身使用线程池并按 `as_completed` 收集结果（[`article_generation.py` L90-L132](https://github.com/stanford-oval/storm/blob/fb951af7744dab086e34962e9bc6fe878e145f83/knowledge_storm/storm_wiki/modules/article_generation.py#L90-L132)），这正提示实现必须显式恢复确定顺序；
- polish 是候选 revision，不是原版本的就地覆盖；fresh-context reviewer 只产出 review finding，不拥有新 Writing identity。

不能照搬的部分：STORM 自己明确说明输出是 Wikipedia-like、不能直接达到 publication-ready，适合预写作而非最终论文（[`README.md` L32-L47](https://github.com/stanford-oval/storm/blob/fb951af7744dab086e34962e9bc6fe878e145f83/README.md#L32-L47)）。其 section prompt 只是把检索 snippet 编号成 `[1]...[n]` 再要求模型内联引用（[`article_generation.py` L136-L175](https://github.com/stanford-oval/storm/blob/fb951af7744dab086e34962e9bc6fe878e145f83/knowledge_storm/storm_wiki/modules/article_generation.py#L136-L175)），不等于 #126 所需的 RG formal citation acceptance，也不能在冻结 Research Snapshot 后继续实时搜索并悄悄扩充证据集。

### 2. Paper：citation 必须来自受限证据集合，并允许明确“证据不足”

PaperQA2 的默认流程是 paper search → gather evidence → generate answer；gather 阶段对 chunk 检索、摘要、评分和重排，再把选中的 context 给生成器（[`README.md` L224-L242](https://github.com/Future-House/paper-qa/blob/57e89f7223b0960d5ee5ea048c69e3c47e088572/README.md#L224-L242)）。它的 prompt 有三个很值得迁移的约束：

- evidence summary 保留具体数字、方程或标明的直接引语，而不是只留泛化摘要（[`prompts.py` L3-L16](https://github.com/Future-House/paper-qa/blob/57e89f7223b0960d5ee5ea048c69e3c47e088572/src/paperqa/prompts.py#L3-L16)）；
- revision 只能使用本轮 context 中仍存在的 key，不能从上一个答案偷带已经失效的 context（[`prompts.py` L30-L36](https://github.com/Future-House/paper-qa/blob/57e89f7223b0960d5ee5ea048c69e3c47e088572/src/paperqa/prompts.py#L30-L36)）；
- 生成器只能引用 context 中的 citation key，证据不足则返回明确的 `I cannot answer` sentinel（[`prompts.py` L52-L68](https://github.com/Future-House/paper-qa/blob/57e89f7223b0960d5ee5ea048c69e3c47e088572/src/paperqa/prompts.py#L52-L68)）。

PaperQA2 还为文档保存内容 hash，而不是只保存文件路径（[`types.py` L75-L88](https://github.com/Future-House/paper-qa/blob/57e89f7223b0960d5ee5ea048c69e3c47e088572/src/paperqa/types.py#L75-L88)）。对 #126，citation allowlist 应进一步收紧为冻结 Snapshot 内、精确 AssetVersion、RG 已 accepted、且 RM 当前 custody 可读的 evidence refs；`citation_key` 必须能回到 source asset hash 与 locator。

但 PaperQA2 的相关性摘要和分数仍是模型判断，不是形式接纳；其 citation prompt 还把 `datetime.now().year` 注入 prompt（[`prompts.py` L83-L90](https://github.com/Future-House/paper-qa/blob/57e89f7223b0960d5ee5ea048c69e3c47e088572/src/paperqa/prompts.py#L83-L90)）。因此可以采用“受限 key + insufficient evidence”协议，不能把 LLM relevance score、自动生成 citation string 或运行当天日期升级为 RG authority 或 deterministic renderer 输入。

### 3. Evidence：先保存可审计关系，再压缩成论文或 slides 文案

GitHub 的 `build-evidence-map` Skill 要求把推理原子化为 `position`、`claim`、`evidence`、`unknown`，并把边限定为 `supports`、`contradicts`、`qualifies`、`missing`；每个 source 还要有日期、精确 locator 和可核查 excerpt（[`SKILL.md` L16-L42](https://github.com/github/awesome-copilot/blob/4742f265959bf025882314564b364d9d7af6e2d5/skills/build-evidence-map/SKILL.md#L16-L42)）。这比“全文里出现了几个引用标号”更接近 #126 的内容／证据／citation 三轴比较。

建议把它迁成 Writing 内部的 `ClaimEvidenceLedger`，至少包含：

| 字段 | 作用 |
|---|---|
| `claim_id`, `claim_text`, `scope` | 稳定识别一个可核查主张，限制它的适用范围 |
| `evidence_ref`, `asset_version_hash`, `locator` | 回到冻结、不可变的 RM 资产与精确区域 |
| `relation` | `supports`／`contradicts`／`qualifies`／`missing` |
| `usage_sites` | paper section／paragraph 或 PPT slide／notes 中的稳定位置 |
| `rg_citation_decision_ref` | 与 Writing 草稿分离的 RG formal decision |
| `current_materialization` | 当前 custody 是否能 materialize，不覆盖历史 acceptance |

该 Skill 的 validator 对 canonical JSON 计算 SHA-256 receipt（[`validate.mjs` L13-L29](https://github.com/github/awesome-copilot/blob/4742f265959bf025882314564b364d9d7af6e2d5/skills/build-evidence-map/scripts/validate.mjs#L13-L29)），并明确说结构验证不证明来源质量或推断为真（[`SKILL.md` L95-L107](https://github.com/github/awesome-copilot/blob/4742f265959bf025882314564b364d9d7af6e2d5/skills/build-evidence-map/SKILL.md#L95-L107)）。#126 应保留这一边界：Writing lint 可证明 ledger 完整、引用可追踪；只有 RG 能给 formal citation acceptance。

### 4. Paper renderer：语义源与格式 Adapter 分离

Quarto 把正文 citation key、bibliography 数据和可选 CSL 样式分开，并由 Pandoc 生成多种格式的引用与 bibliography（[`citations.qmd` L9-L44、L72-L100](https://github.com/quarto-dev/quarto-web/blob/a6b7948a63daaef1fe9b2b5cf49b0144a6901528/docs/authoring/citations.qmd#L9-L44)）。这支持一个重要设计：Writing 的 canonical source 不应是 DOCX／PDF／PPTX 文件，而应是带稳定 citation refs、figure/table refs 和语义结构的 IR；格式规则、CSL、期刊模板只是 renderer inputs。

Quarto 的 `freeze` 会缓存 computation 结果并建议纳入版本控制，但文档也提醒外部输入数据变化时仍需重新执行（[`code-execution.qmd` L21-L31](https://github.com/quarto-dev/quarto-web/blob/a6b7948a63daaef1fe9b2b5cf49b0144a6901528/docs/projects/code-execution.qmd#L21-L31)）。所以 `freeze` 只能作为“冻结计算结果”的启发，不能替代 #126 的 immutable Research Snapshot、RM custody 或 byte-level renderer determinism。

### 5. PPT：先规划叙事与 archetype，再做几何布局

OpenAI Google Slides Skill 给出的顺序是：读内容与目标模板 → 定义每页要表达什么 → 盘点文本、图片、图表、链接、媒体、notes → 映射 slide archetype → 先做代表性子集 → thumbnail 验证 → 扩展全 deck → 全局一致性检查（[`slide planning` L5-L29](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/google-drive/skills/google-slides/references/reference-slide-planning-and-layout-selection.md#L5-L29)）。大型 deck 先做 title、section divider、dense slide、visual slide 和一个高风险 evidence slide，再同时验证内容提纲与 archetype mapping（[`同文件` L58-L73](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/google-drive/skills/google-slides/references/reference-slide-planning-and-layout-selection.md#L58-L73)）。

可迁成以下 `SlidePlan`，仍属于同一 Writing AssetVersion：

```text
deck_purpose / audience / intended_decision
narrative_spine[]
slides[]:
  stable_slide_id
  rhetorical_job        # opening / question / evidence / comparison / synthesis / appendix ...
  takeaway              # 本页希望受众记住的一句话
  claim_ids[]
  evidence_refs[]       # 含 locator，不是自由文本 URL
  visual_archetype
  source_asset_refs[]   # 图片、图表、表格的不可变资产
  speaker_notes
  density_risk / qa_status
```

“一页一个主要修辞任务”应是可审查的 editorial heuristic，而不是把所有 slide 强行固定为同一种 card／标题+三 bullet 模板。证据密度高时应拆页、转 appendix 或转 notes，不能为了版面简洁删除图例、locator、限定条件或反证；OpenAI Skill 同样要求保留图表的 labels、legends、footnotes 与证据含义（[`slide planning` L87-L93](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/google-drive/skills/google-slides/references/reference-slide-planning-and-layout-selection.md#L87-L93)）。

### 6. PPT／paper：内容、包结构、视觉必须分别验收

Anthropic PPTX Skill 把 QA 分成 content extraction、Office package validation 和逐页 visual inspection；其视觉检查明确覆盖 overflow、overlap、citation/footer 碰撞、边距、对齐、对比度和残留 placeholder（[`pptx/SKILL.md` L166-L219](https://github.com/anthropics/skills/blob/3b3fad96af16a10759d930941b4520ba0c40edae/skills/pptx/SKILL.md#L166-L219)）。DOCX Skill 也要求先转 PDF／图片后目视检查，再运行 OOXML validator（[`docx/SKILL.md` L35-L58](https://github.com/anthropics/skills/blob/3b3fad96af16a10759d930941b4520ba0c40edae/skills/docx/SKILL.md#L35-L58)）。这些是功能性观察；其许可证禁止复制或衍生，#126 必须自行实现等价 gate。

OpenAI Skill 补充了两个关键边界：

- API write 成功不能证明视觉正确；每次 patch 后必须抓取全新 thumbnail，再检查、修复和复验（[`thumbnail verification` L1-L62](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/google-drive/skills/google-slides/references/reference-thumbnail-visual-verification.md#L1-L62)）；
- thumbnail 也不能证明空 placeholder、speaker notes 或 required-content fidelity，必须与结构化 readback 配对（[`同文件` L5-L7](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/google-drive/skills/google-slides/references/reference-thumbnail-visual-verification.md#L5-L7)）。最终 readback 还明确拒绝未获用户要求的“整页 PNG 组成的 deck”（[`final pass` L20-L32](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/google-drive/skills/google-slides/references/reference-new-deck-and-final-pass.md#L20-L32)）。

因此 #126 的 renderer QA 至少应有四条彼此独立的 lane：

1. **semantic QA**：citation allowlist、dangling claim／evidence、section／slide 顺序、图表／notes／locator 完整性；
2. **package QA**：OOXML schema、relationships、content types、可打开性、嵌入媒体与字体声明；
3. **visual QA**：固定 renderer rasterize 每页／每 slide，逐页检查后再做 contact sheet 总览；
4. **determinism QA**：同一封闭输入在隔离环境生成两次，最终封存字节 SHA-256 必须一致。

机器通过这些 QA 只证明 artifact contract；不应自动改写 RM acceptance、RG citation decision 或代替用户对内容／视觉的正式验收。

## 建议的 #126 Writing Skill 流程

以下各步骤运行在 #125 已存在的一个 managed Writing Run／root native Session 与同一版本 lineage 内；短命 child reviewer 只提交 finding。

| 步骤 | 产物／gate | 失败时的真实状态 |
|---|---|---|
| 1. 冻结输入 | 精确 Writing Intent、deliverable type、受众／用途、Research Snapshot ref/hash | 不创建 paper／PPT 草稿 |
| 2. 构建 evidence allowlist | Snapshot 内的 source AssetVersion、hash、locator、RG 状态、当前 RM custody | 缺 custody 为 `asset_custody_unavailable`；RG rejected 不可进入 formal draft/render |
| 3. 内容规划 | paper 的 genre-aware section plan；PPT 的 narrative spine + `SlidePlan[]` | 缺 claim coverage 或关键 unknown，保留 planning checkpoint |
| 4. 起草 | canonical semantic IR + `ClaimEvidenceLedger`，所有 citation 只用 allowlist key | 证据不足显式 finding，不准补写无来源事实 |
| 5. 自检与 fresh-context review | content、evidence、citation、结构、可读性 findings | 形成 successor revision；不就地改写历史版本 |
| 6. RM 接纳 candidate | 不可变 paper／PPT semantic `AssetVersion` | execution completed 仍不等于 deliverable accepted |
| 7. RG formal citation decision | 与精确 AssetVersion 绑定的 accepted／rejected decision | rejected 可修订重提，但不可 formal render |
| 8. deterministic render | renderer manifest + 正式 artifact bytes/hash + package/visual receipts | 任一输入漂移、custody 丢失或重复生成 hash 不同均 fail closed |
| 9. Web preview／compare／download | 内容／证据／citation 三轴比较、stale/currentness、逐页预览 | preview 不是 publish；历史继续可读 |
| 10. 外部交付 | 精确版本、artifact hash、目标与权限绑定的 HC Preview／confirmation；AR operation/reconciliation receipt | 未确认是 `not_attempted`；未知结果先对账，禁止盲重试 |

### Paper 类型特有 gate

- 论文结构必须按文类／用途配置：实证论文、方法论文、综述、理论／观点稿不能被一个固定 IMRaD 模板冒充完整领域合同。
- 每个 material factual／quantitative claim 必须链接至少一个允许的 evidence ref，或者显式标为 `unknown`／unsupported finding；反证与限定证据不能被 polish 静默删除。
- citation formatter 只消费 RG 已 accepted 的 canonical reference metadata；不得让模型凭题名自由补 DOI、卷期页码或访问日期。
- figure／table 必须有稳定 ID、caption、source asset hash、正文引用与可访问性信息；检查 orphan figure/table、断裂 cross-reference、孤儿 bibliography item、坏分页、截断表格和不可读脚注。
- renderer template、CSL、locale 都是可版本化输入，不是正文事实；换模板只产生新 renderer artifact，不产生新内容版本或 citation decision。

### PPT 类型特有 gate

- 先验证 narrative spine 和代表性高风险子集，再批量生成；避免整套 deck 完成后才发现 archetype 与证据密度不匹配。
- 每页保存稳定 `slide_id`、rhetorical job、takeaway、claim/evidence refs、visual archetype、speaker notes；标题和页码不能充当 identity。
- data claim、chart、image、table 的来源与 locator 必须在 slide 或 notes 中保持可追踪；视觉简化不能删除图例、单位、限定条件或不确定性。
- package 结构和 rasterized thumbnail 都要复验；contact sheet 只做最终 sweep，不能替代逐页大图检查。
- 正式 PPT 默认应保留原生文本、结构与 notes；只有用户／上游合同明确要求 image-only deck 时，才可把整页 raster 当正式输出。

## Deterministic artifact 生成：必须补上的工程约束

Quarto、PptxGenJS、Marp 都能“可重复执行”，但这不自动等于“同一输入得到完全相同的正式字节”。最直接的证据是 PptxGenJS 4.0.1 在 `docProps/core.xml` 的 `created`／`modified` 字段调用 `new Date()`（[`gen-xml.ts` L1514-L1524](https://github.com/gitbrent/PptxGenJS/blob/3c9ec1b687c174952166f6a34b5e87ebf69fa469/src/gen-xml.ts#L1514-L1524)），嵌入 chart workbook 也写入当前时间（[`gen-charts.ts` L85-L97](https://github.com/gitbrent/PptxGenJS/blob/3c9ec1b687c174952166f6a34b5e87ebf69fa469/src/gen-charts.ts#L85-L97)）。直接对原始输出做 SHA-256 因而不能满足 #126 的 deterministic formal renderer。

建议 renderer 以如下 manifest 作为完整输入：

```text
render_contract_version
deliverable_asset_version_ref + content_hash
rg_citation_decision_ref + decision_hash
semantic_ir_hash
template_asset_hash
csl_or_theme_hash
renderer_binary_or_container_digest
font_bundle_hash
locale + timezone
explicit_seed
normalization_version
```

并执行以下 fail-closed 规则：

1. renderer 阶段禁止网络读取；图片、字体、bibliography、模板和计算结果必须先成为受管 Snapshot／RM bytes；
2. stable section／slide／relationship ID 由 canonical input path 派生，不使用随机 UUID；无序 map 在序列化前排序；
3. 固定 locale、timezone、seed、字体包和 renderer/container digest；缺字体不能静默 fallback；
4. 固定或规范化 OOXML core properties、嵌入 chart workbook metadata、ZIP entry 时间、entry 顺序和压缩配置；
5. 规范化完成后再封存最终文件，`renderer_hash` 必须是用户实际下载字节的 hash，而不是忽略差异的“语义 hash”；
6. 在两个干净进程中各渲染一次并比较最终 SHA-256；不一致返回 typed nondeterminism blocker，不得创建第二个正式 artifact；
7. `artifact_identity = hash(上述 manifest)`；相同 identity 的 render／preview／download 返回已经封存的同一 artifact，不重新创建 RM AssetVersion；
8. custody 丢失时保留历史 manifest／hash／RG acceptance 的可见性，但 materialize、render、download 和外部交付全部以 `asset_custody_unavailable` fail closed。

PptxGenJS 可以作为 MIT 许可的 OOXML生成候选，因为它支持原生 text、table、shape、image、chart 与 master（[`README.md` L11-L45](https://github.com/gitbrent/PptxGenJS/blob/3c9ec1b687c174952166f6a34b5e87ebf69fa469/README.md#L11-L45)），但必须被上述 normalization／validation wrapper 包住；不能把库的 `writeFile()` 成功当作 deterministic receipt。

## 明确不应迁入本产品的部分

1. **实时搜索直接进入 Writing。** STORM／PaperQA 的 search loop 可以启发 Research 阶段，但 #126 Writing 只能消费已冻结 Snapshot；后续研究进展不应悄悄改变正在写的版本。
2. **把模型 relevance score 当 citation acceptance。** PaperQA 的 scored summary 是检索启发，不是 RG authority；“有 citation key”也不证明 claim 被 source 支撑。
3. **把固定论文模板当论文事实。** OpenAI plugins 中的 Hugging Face `arxiv.md` 模板直接含示例 baseline 数字、“state-of-the-art”结论、固定硬件与伪 references（[`arxiv.md` L107-L178](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/hugging-face/skills/paper-publisher/templates/arxiv.md#L107-L178)）。这种模板最多能提供空 section scaffold，绝不能进入候选稿、测试 expected content 或 production default。
4. **authoring Skill 直接执行 publish。** 同一 HF script 在 `--create-pr` 未实现时打印提示后仍直接 `upload_file` 提交（[`paper_manager.py` L119-L177](https://github.com/openai/plugins/blob/11c74d6ba24d3a6d48f54a194cd00ef3beea18f9/plugins/hugging-face/skills/paper-publisher/scripts/paper_manager.py#L119-L177)）。这违反 #126 的精确 Preview／confirmation、稳定 operation identity 与 unknown-outcome reconciliation，必须完全排除。
5. **把 image-only deck 伪装成正式 PPT。** Marp 默认 PPTX 由预渲染 slide images 构成、内容不可复用／编辑；它的 editable PPTX 仍标为 experimental，复杂样式可能失败，外观复现更低且不支持 notes（[`Marp README` L174-L205](https://github.com/marp-team/marp-cli/blob/527edc3b30826cffe021ef05cc4812227a035ccc/README.md#L174-L205)）。它可作为 preview／对照 renderer，不能默认为正式 PPT contract。
6. **把 cache／freeze 当 byte determinism。** 缓存只说明复用上次 computation；未固定外部数据、工具链、字体、时间和包元数据时，仍不能给 deterministic artifact receipt。
7. **把 mutable Google Slides／PowerPoint 文档当 RM authority。** 外部编辑器 object ID、thumbnail 和 Provider ACK 都只是执行／观察证据；不可替代同一 Writing AssetVersion、RM custody 或 RG decision。
8. **复制许可证不允许的 Skill。** Anthropic PPTX／DOCX 是 proprietary，OpenAI plugins 的相关 Google Drive／Hugging Face 子树在固定 commit 未声明可复用许可证；本产品只能独立实现抽象工作流，不能复制其文字、代码、模板或资产。
9. **用单一示例冻结产品类型。** 一个 `arxiv` 模板、一个 PPT archetype 集或一个 renderer 只能作为 Adapter／验证样本；不得把 paper 固定为一种学科／期刊结构，或把 PPT 固定为一种视觉模板。

## 对实现票的可追踪建议

| #126 合同关注点 | 本调研给出的实现启发 | 最小公开 seam 证据 |
|---|---|---|
| 同一 Writing lineage 增加 paper／PPT | canonical semantic IR + type-specific plan／lint／renderer Adapter；不建新 Owner | Web 中 report／paper／PPT 共享 Run、root Session、版本 refs 与三轴 compare |
| 引用与证据约束 | frozen evidence allowlist + `ClaimEvidenceLedger` + RG decision ref | Web 可从 claim／slide／section 下钻到精确 AssetVersion、hash、locator 与 decision |
| deterministic renderer | 完整 manifest、固定工具链、OOXML／ZIP normalization、双渲染 hash gate | 同 accepted version 重复 render／preview／download 返回同 artifact ref/hash；注入时间漂移测试 fail closed |
| PPT 结构与视觉 | narrative spine → archetype → representative subset → per-slide fresh render QA | 逐页结构 readback、thumbnail、package receipt；Chrome 中可见修订前后结果 |
| paper 结构与视觉 | genre-aware outline → section evidence budget → draft/review → page render QA | citation allowlist、figure/table/crossref lint、逐页 preview 与 immutable revisions |
| custody 丢失／恢复 | 历史 acceptance 与当前 materialization 分离 | 历史仍可读；render/download/delivery 返回 `asset_custody_unavailable`；恢复原字节不新建版本 |
| 外部 delivery | authoring／renderer 无副作用；HC exact confirmation 后才由 AR 执行并对账 | `not_attempted`、partial、outcome unknown、completed 分开展示，ACK 不冒充整体成功 |

## 仍需 HITL／后续合同固定，而不应由本票偷定的事项

- paper 的正式输出集合：PDF、DOCX、LaTeX source、JATS 或其他格式；
- PPT 是否必须原生可编辑、允许哪些不可编辑 visual，以及 speaker notes 的正式要求；
- 期刊／机构模板、CSL、品牌主题和字体包的来源、许可证与分发边界；
- 可安装产品实际支持的外部 publish／send／submit Provider；
- 哪些 visual QA finding 是自动 release blocker，哪些需要领域／设计人员正式验收；
- Quarto／Pandoc、PptxGenJS 或其他 renderer 是直接依赖、独立进程 Adapter，还是仅作为 conformance oracle。尤其 Pandoc 的 GPLv2 分发边界应单独做许可证审查，不能由这份设计调研代替。

在这些选择被固定前，生产合同应保留 Adapter seam，并用 deterministic fake／sandbox 证明协议、恢复与状态边界；不得据此宣称未实现的真实格式或 Provider 已可用。
