# 论文类型：`paper-v1`

形成研究论文候选，遵循主 Skill 的冻结 Snapshot、引用标记、lineage 和定稿流程，保持同一根 native Session。主 Skill 中的“文档”在此指论文；不新增论文专属顶层会话。

## 语义结构

按 Intent 选择实证、方法、综述、理论或其他合理体裁，不固定套用 IMRaD。每个 H2 前给出稳定语义角色，可见标题及语言按读者需要：

```text
<!-- meta-research-paper-section role=<role> -->
```

合法角色为 `abstract`、`framing`、`related-work`、`methods`、`model`、`evidence`、`results`、`analysis`、`synthesis`、`evaluation`、`discussion`、`limitations`、`implications`、`conclusion`、`appendix`。开头有一个 `abstract` 和一个 `framing`；至少一个核心论证角色（`methods` 至 `evaluation` 中适用项）、至少一个限定角色（`discussion | limitations | implications`）及一个 `conclusion`，结论后仅可有附录。使用 5–24 个唯一且有实际内容的 H2。

每节至少含一个可分类主张块。语义角色是写作协议，不是固定英文标题或 Word 样式。摘要说明有边界的问题、方法、主要结果和限制；framing 说明缺口和贡献；核心章节使证据与论证可核查；限定章节处理反证、范围和效度威胁；结论只回答冻结证据支持之处。作者、单位、日期和期刊信息仅按真实输入填写，不补造书目。

渲染器从已接纳语义源生成 DOCX；此阶段交接语义 Markdown，不输出 OOXML、base64、HTML 或下载链接。

## 论文复核

除共享标准外，核对章节角色、方法与结果区分、摘要结论一致性、反证、效度威胁和贡献范围。完成条件：论证结构及引用可核对；结构合规不等于 RG 已接纳引用，也不证明可直接发表。
