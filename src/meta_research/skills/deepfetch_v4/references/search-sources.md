# 本次运行的补充来源

宿主为整个逻辑 Run 固定清单中的来源配置版本，每轮关联当前实际通用 MCP 快照。新保存的配置只供新 Run 使用。初始化首题、手动 Question 与后续自主研究使用同一入口。清单和测试报告只说明允许范围与能力，不证明研究使用。

通过 `deepfetch_source_action` 调用清单中具体 `source_id`。API 使用 `operation=api_search`、`query`、可选 `limit`；网站使用 `operation=website_open`、同源 `url`；直接 MCP 使用 `operation=mcp_tool_call`、清单目录的 `tool_name` 与匹配实际 schema 的 `arguments`。不传 manifest、Run 或 job；宿主从已验证的运行和轮次确定归属。凭据由宿主使用，不索取、不写入提示或材料。

返回 `receipt`、`records`、`content`、`limitations`。保留实际请求、结果性质和 `receipt_ref`。网站页面可读不证明站内搜索、动态渲染或论文全文；MCP 目录与不透明调用结果仅作为实际调用线索，不能自行编造论文标识或版本。摘要、网页线索与独立 Reader 阅读过的原论文分别处理。

核实稳定论文标识和明确版本后，在 `papers.py upsert` 输入中填写 `paper_version` 与 `discovery_origins=[{"receipt_ref":"宿主回执"}]`。回执必须实际返回同一 DOI 或 arXiv 与同一版本。宿主在导入时验证结果成员关系并保存所有已验证发现来源。若实际结果缺版本或身份，不把该回执声称为已确认论文发现；保留为有限线索并继续核验。版本不明使用 `unverified`，不能自动跨源合并；预印本、正式版和 arXiv v1/v2 分别登记。

失败、空结果、限流、登录或目录不可用均保留具体限制，继续其他可用来源。用户明确必用的来源无法满足时，说明影响并沿当前 Agent 的 HumanRequest 求助。补充来源不能替代原生 Web Gate。入选论文仍交共同 Acquisition、独立 Reader 和 RM 公开快照。
