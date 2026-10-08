# 微信搜狗研究线索

OpenAlex、微信搜狗和原生 Web 是并列发现渠道。按问题组织中文及其他适当查询，已有 OpenAlex 命中仍可使用搜狗。按新增证据价值停止，不规定每次命中或文章数量。provided_only 禁止新发现，两个搜狗工具不授予且宿主再次拒绝调用。

## 工具

调用 Meta MCP `deepfetch.sogou.search`，精确参数为 `{"query":"实际研究查询"}`。返回 `receipt`、`candidates`、`article_body`。每个 candidate 恰含 `candidate_ref` 和 `receipt`。从相关结果选择并调用 `deepfetch.sogou.open`，精确参数为 `{"candidate_ref":"该次返回的引用"}`。open 返回同形对象，candidates 为 []，article_body 为实际提取正文或 null。不能传任意 URL。引用只属于本次 Attempt，20 分钟或进程重启后需重新搜索。

宿主使用新建内存 HTTP session，不读取个人 cookies 或浏览器 profile。它支持普通 HTTP 跳转和实测的固定 `url +=` 片段到微信文章路径，遇不支持脚本则停止。不执行任意 JavaScript、不解验证码。HTTP 200 可能仍是登录或验证码。

## 回执

每个 receipt 恰含 `receipt_ref`、`channel`、`action`、`query`、`parent_receipt_ref`、`observed_at`、`outcome`、`observation`、`evidence_kind`、`excerpt`、`limitation`。搜狗 channel 为 sogou_wechat。其他可核验渠道可记录 openalex、native_web、provided，引用真实工具或材料证据，不能冒充宿主搜狗执行。

observation 恰含 `requested_url`、`returned_url`、`final_url`、`redirects`、`http_status`、`title`、`account_or_author`、`published_at`、`content_sha256`。未知标量为 null，未知集合为 []。搜索 URL、实际返回的中转 URL、最后实际访问 URL 分开记录。署名和标题来自当前证据，搜索卡片署名不代表正文署名已核验。

outcome 为 `results | opened | empty | captcha | login_required | rate_limited | expired | unavailable | unsupported_redirect`。限制附具体 limitation，并保留原搜索片段。evidence_kind 为 result_snippet、opened_article_body 或 academic_source。opened_article_body 必须为实际微信正文，具有正文 hash 和 excerpt。academic_source 是原学术来源核验，仍不是 independent Reader。

把 search 的 receipt、每个实际返回 candidate 的 receipt 及 open 的 receipt 原样加入同一 papers.json 的 discovery.receipts。open 引用对应卡片的搜索回执。使用 [台账工具](ledger-tools.md) record-discovery，不把回执文件作为另一份公开论文台账。宿主导入拒绝未知或篡改的搜狗回执。

公众号与项目仅是线索。沿实际文章引用核验原论文稳定学术身份与版本后再 upsert。未核实原论文放入 discovery.unresolved_leads，并记录具体限制。只有已获取原论文及独立 Reader 可以声称论文已读。

## 受限时继续

搜狗局部失败后继续 OpenAlex 和原生 Web。原生 Open/Fetch 若真实读到同一微信地址的正文，记录其独立 native_web 回执与实际调用依据，保留宿主 captcha 原始回执。不能把两种访问事实合为宿主 opened。原生 Search 加 Open/Fetch Gate 继续按既有流程完成，搜狗结果不能替代它。
