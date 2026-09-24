---
name: writing-report
description: 根据一个冻结的 Meta Research Quest Snapshot 起草和修订有来源的 Markdown 研究文档；用于 Writing Run 草稿、根会话引用定稿及按反馈形成后继版本，不负责领域接纳、发布或推进研究阶段。
---

# 研究文档写作

在受管 Writing Session 内形成可核查候选。以 Intent、Snapshot、runtime binding、lineage 和反馈为精确输入。研究可继续接纳新事实，但本 Run 只使用 HC 授权时封存的切面；新的事实属于后续 Snapshot。写作不推进、暂停或阻塞研究 Stage。丢失响应或重启继续原切面，不重新捕获或替换成最新状态。

用户可见标题和正文按当前 `zh`／`en` 偏好直接撰写，未提供偏好时遵循 Intent；子智能体继承要求。原始引文、标识和下文机器识别的固定标记保持原样。本文为中文不表示研究文档必须中文。

## 1. 读取来源并起草

按题名、读者、用途与要求撰写有效 Markdown。形成证据主张前读取 `accepted_source_manifest` 指定文件，并绑定回其 `version_ref`；路径只是传输位置，不是引用身份。资料多时可委派聚焦检索或资产回顾，子智能体在同一冻结来源范围返回精确入口、理由、边界和未决项，根 Session 抽查采用的关键原文。

当前授予的 Web／MCP、单项 Acquisition 或 HumanRequest 可用于识别证据缺口及后续研究线索；其新增结果只有被后续授权 Writing Intent 的 Snapshot 收录后，才可成为该次正式写作证据。本 Run 的主张与引用仅来自当前冻结来源根和 manifest，不能用其他工作区或实时 Owner 文件替换。

完成条件：采用的每个来源都在精确 Snapshot 中，所需原文已读，缺证据处明确标出。

## 2. 表达主张和引用

首块只能是唯一 H1 标题，后续每块以一个明确标记开始。章节标题用 `<!-- meta-research-structure -->` 紧接一个 H2–H6。支持主张用 `<!-- meta-research-claim:supported refs=<citation_ref,...> -->`，同块放入各匹配 `[[citation:<citation_ref>]]`。其余块分别用 `<!-- meta-research-claim:inference -->`、`<!-- meta-research-claim:uncertainty -->`、`<!-- meta-research-claim:evidence-gap -->`，可见正文必须以协议固定前缀 `**Inference:**`、`**Uncertainty:**`、`**Evidence gap:**` 开始；后续解释按用户语言撰写。

仅引用 `accepted_sources` 中的 `version_ref`。每个支持块的各锚点返回一条 citation，包含精确来源版本、locator、去掉锚点后的完整块文本 `claim` 和逐字来源片段 `source_quote`。当前 RG 要求规范化 claim 与该片段相同，且片段确实位于 locator。翻译、转述和多来源综合放入明确的推断或不确定性块，不能伪装成已逐字验证原文。书目信息、引文和定位均取真实来源。

locator 恰用以下形式之一：单文件 UTF-8 文本 `line:<1-based line>`；单 PDF `page:<1-based page>`；目录／仓库文本 `path:<percent-encoded portable path>#line:<1-based line>`；目录内 PDF `path:<percent-encoded portable path>#page:<1-based page>`。条目路径由 staged manifest 选择，不使用本地传输绝对路径。

完成条件：每个块的主张类别明确，锚点、来源版本、原文和定位互相一致；缺口保持显式，不补造引用。

## 3. 独立审阅与根会话定稿

草稿持久保存后，在同一根 Session 的第二个 provider 回合组织原生独立子智能体审阅完整草稿与关键原文，检查证据覆盖、引用、过度确定性、内部一致性和 Intent 对齐。子智能体返回自由格式反馈，根 Session 负责采纳、修订和最终内容；同根自查不代替独立审阅。

当前 Writing adapter 的输出 schema 仍要求 `findings` 与相应 `revised | not_adopted` disposition；按实际反馈填写，`revised` 须对应 Markdown 或引用集合的实质变化。`review_mode=advisory_unobserved`、`reviewer_agent_ref=null` 和中性任务 hash 由 adapter 持久化；它们不构成 Owner 对原生子会话身份的认证，也不允许伪造审阅记录。根 native Session 始终保持同一身份。

RG 或人类反馈后，在同一 Session 按 predecessor lineage 修订，生成后继内容，保留先前已接纳版本。完成条件：真实审阅已进行，最终稿反映主 Agent 的判断，引用和文档类型要求通过核查；格式通过不等于正式引用接纳或发布。

## 停止与权限边界

Snapshot hash、运行绑定、Session、Fence、predecessor 或来源绑定不符时停止并报告具体问题。证据不足可交付明确缺口，权限或完整性故障保留阻塞。暂停／取消服从宿主控制，恢复沿现有 Run，不另开顶层 Session 绕过。

内容接纳、receipt、正式渲染、研究阶段推进和对外发布由对应 Owner／后续流程负责，写作 Skill 只提交候选。
