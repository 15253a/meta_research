---
name: nature-downloader
description: 按 DeepFetch 已选定的有限论文清单，经合法开放获取、出版商 API、CNKI 或已认证机构浏览器取得并验证全文；不负责发现、排序、科学阅读或综合。
---

# Nature Downloader：DeepFetch 全文获取服务

只获取本次明确请求的论文，返回简洁交付证据。DeepFetch 提供精确标题、标识、URL 线索与绝对私有获取目录；选题、科学阅读及公开产物由 DeepFetch 负责。用户可见说明遵循当前根 Session 的语言偏好，原文和协议值保持原貌。

## 当前请求与路线

每个 `request_id` 是独立路由事务，即使 Acquisition Session 在整个 Quest 中持续存在。按当前请求明确的 `session_mode` 执行，不沿用上一回合的临时模式：`oa_then_institution` 先 OA 再已授权机构路线，`oa_only` 只做 OA，`provided_only` 只验证用户已提供的正文。`route_policy=oa_first_then_institution` 是协议中的路线次序，不能覆盖 `session_mode` 的权限限制。

获取服务负责合法路由、传输、内容验证、hash、typed 失败和私有 manifest，只写调用方指定目录。已选正文获取始终传 `--no-si` 和 `--cnki-format pdf`，无需再次确认补充材料；DeepFetch 接受核实的 PDF、HTML、XML 正文，CAJ 不适用。

## 1. 预检

`oa_only` 跳过全部机构／浏览器预检，并传 `--no-institutional-access`；这是已选主路线，不描述为被迫降级。`provided_only` 仅核实已提供文件，不发起网络下载。需要机构路线时，在 DeepFetch 开始主动检索计时前检查配置、连接和真实权限：

```bash
python3 scripts/configure_school.py show
python3 scripts/configure_school.py health --force
node scripts/functional_cdp_probe.mjs --proxy http://127.0.0.1:3456
```

先检查已有受控浏览器页面。显示机构身份的出版商正文页，或核实的付费正文响应才能证明访问权限；图书馆首页和 `/targets` 可达只证明连接。新开登录页不推翻其他已授权页面。

代理 `new`／`eval` 失败而原生 Chrome CDP 可用时，在未占用回环端口启动随包桥接并重新功能探测，所有机构请求传同一 `--proxy`。原生 CDP 已在 9222、预期代理缺失时可复用：

```bash
node scripts/direct_cdp_proxy.mjs --chrome http://127.0.0.1:9222 --port 3456
curl --noproxy '*' --max-time 10 http://127.0.0.1:3456/targets
```

桥接复用现有浏览器，仅暴露页面操作。凭据、cookie、local storage 和用户配置文件留在受控环境，不进入 Agent 上下文。机构路线仍不可用时给出具体登录待办、OA-only 继续或取消选项；纯用户等待不计主动检索时间。完成条件：当前模式已明确，所需访问已核实或有明确等待原因。

## 2. 获取

远程下载统一经代理感知启动器，启用已配 HTTP(S) 代理并绕过回环浏览器控制：

```bash
python3 scripts/run_batch_download.py <batch_download arguments>
```

每个论文和路线尝试使用 `target_dir` 下唯一目录并保留 manifest，不跨标题复用 `--out`。`provided_only` 直接进入验证；其他模式先做 OA。

### OA 轮次

对每篇请求先尝试合法 OA。标题和 DOI 均用于查找；优先检查已有 arXiv ID、PMC、仓储或显式 PDF 等 `source_urls`，将线索视为未核实输入并验证所得论文。候选或传输失败不结束整个 OA 轮次，直到得到核实正文或适用候选耗尽。

直接候选用 `--pdf-url "<url>" --title "<exact title>" --no-institutional-access --no-si`，每次独立目录；精确标题示例：

```bash
python3 scripts/run_batch_download.py \
  --title "精确论文标题" --open-access --no-institutional-access \
  --no-si --cnki-format pdf \
  --out "/absolute/private/acquisition-dir/attempts/<paper-id>/title-oa-1"
```

工具会按精确标题解析迁移的 DSpace 条目。每个获得正文都先验证，再从未决清单移除。`oa_only` 到此为止，不进入机构路线。

### 机构轮次

仅 `oa_then_institution` 中 OA 仍未解决的论文可进入出版商 API 和已授权机构路线。带 DOI 的未决项可有界批量处理：

```bash
python3 scripts/run_batch_download.py \
  --dois "10.xxxx/one,10.xxxx/two" --api-fallback-web \
  --no-si --cnki-format pdf \
  --out "/absolute/private/acquisition-dir/attempts/institution-batch-1"
```

无 DOI 的精确标题单独提交。OA 成功项不再获取，`--api-fallback-web` 不与 `--no-institutional-access` 并用。中文论文走可授权 CNKI 并要求 PDF；英文合法 OA 可含出版商 OA、PMC、Unpaywall 所列合法副本、arXiv 与仓储，剩余项再按当前授权用出版商凭据和机构浏览器。

仅在所有适用路线均有终态时返回 `missing`。OA-only 耗尽为 `oa_not_found` 或宿主 `acquisition_route_exhausted`，不是机构授权失败；已确认正文位置但字节传输超时为 `transfer_failed`。不得把等待用户当作路线耗尽。

## 3. 核实并返回

manifest 与实际内容需一致：PDF 是有效正文，HTML／XML 含文章主体而非登录、拒绝、落地或验证页，身份与所请求论文匹配。CAJ-only 不算 DeepFetch 正文。每篇返回一项：

```json
{
  "paper_id": "openalex:W123",
  "status": "obtained",
  "path": "/absolute/provider/result.pdf",
  "format": "pdf",
  "failure": null
}
```

`status` 为 `obtained | waiting_user | missing`；`format` 为 `pdf | html | xml | null`。`waiting_user` 保留相关页面和私有重试引用；已认证正文页自动下载失败时，可请用户在该页手动下载或提供正文。`missing` 的路径和格式为 `null`，失败对象为 `{"code":"...","detail":"..."}`，不含凭据和会话材料。DeepFetch 通过确定性台账工具登记成功文件。

完成条件：每个请求都有核实交付、具体人类待办或保留全部适用尝试的路线耗尽终态。受影响论文及时返回，其他 OA、检索和 Reader 工作继续。

需要机构浏览器或登录交接时读取[机构浏览器流程](references/institutional-browser-workflow.md)；需要验证、诊断、隔离、重试或具体失败码时读取[交付验证与失败](references/delivery-verification-and-failures.md)。遇 CAPTCHA、QR、OTP、安全或机器人验证时暂停受影响路线，由用户处理。
