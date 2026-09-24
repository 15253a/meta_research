# 交付验证与失败

传输需要内容验证、隔离、诊断、重试或具体终态时读取。DeepFetch 绑定请求传 `--no-si`，补充材料不在本获取合同内。

## 身份和内容

`obtained` 必须满足适用检查：请求 DOI／规范标题与元数据和正文匹配；返回的是正文而非落地、登录、拒绝、同意、摘要或安全验证页；PDF 有有效文件头并可解析为实质文档，HTML／XML 有文章章节和身份元数据；路径、MIME／格式、字节数、SHA-256 与 `manifest.json` 一致；文件在调用方指定私有目录内。

CAJ、单独补充材料、出版商落地页及错文不算正文。可疑文件留私有诊断区，只有核实 PDF／HTML／XML 可进入公开台账。

## 聚焦重试

临时网络故障、不完整传输或过期签名 URL 可沿同一合法路线重试一次。直接传输得到登录／拒绝页，而受控浏览器可见已授权正文时，改走[同浏览器传输](institutional-browser-workflow.md)。身份错配、无权限、登录过期或安全验证时停止自动路线重试并返回真实状态。

## 失败码

| code | 真实含义 |
| --- | --- |
| `metadata_not_found` | 无法可靠解析 DOI／标题。 |
| `oa_fulltext_not_found` | 适用合法 OA 查找后没有正文。 |
| `institutional_login_required` | 登录缺失或过期。 |
| `library_no_permission` | 机构明确没有全文权限。 |
| `anti_bot_or_security_challenge` | CAPTCHA、QR、OTP、机器人或安全验证阻塞。 |
| `no_authorized_pdf_found` | 要求 PDF 的路线未取得有效 PDF，包括绑定 CNKI。 |
| `full_text_html_available` | 无 PDF 但有核实 HTML，按成功 HTML 返回。 |
| `file_validation_failed` | 字节、MIME、解析或正文检查失败。 |
| `paper_mismatch` | 正文属于另一作品。 |
| `transfer_failed` | 已核实路线经一次聚焦重试仍传输失败。 |

说明保持简洁可观察，不含凭据、cookie、token、签名查询串或浏览器状态。缺全文不证明论文不重要或结论错误。

## 简洁结果

成功返回 `paper_id`、`status=obtained`、绝对私有路径、`format=pdf|html|xml`、`failure=null`；失败返回 `status=missing`、空文件字段和失败对象。具体用户待办用 `waiting_user` 并保留恢复线索。manifest 保持私有，DeepFetch 只记录对研究有影响的限制并经 `papers.py` 登记已核实正文。
