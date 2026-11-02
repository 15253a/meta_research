---
name: nature-downloader
description: 按 DeepFetch 已选定的有限论文清单，经合法开放获取、出版商 API、CNKI 或已认证机构浏览器取得并验证全文；不负责发现、排序、科学阅读或综合。
---

# Nature Downloader：DeepFetch 全文获取服务

只获取本次明确请求的论文，返回简洁交付证据。DeepFetch 提供精确标题、标识、URL 线索与绝对私有获取目录；选题、科学阅读及公开产物由 DeepFetch 负责。用户可见说明遵循运行时既有语言偏好，原文和协议值保持原貌。

## 当前请求与路线

每个 `request_id` 是独立路由事务，即使 Acquisition Session 在整个 Quest 中持续存在。按当前请求明确的 `session_mode` 执行，不沿用上一回合的临时模式：`oa_then_institution` 先 OA 再已授权机构路线，`oa_only` 只做 OA，`provided_only` 只验证用户已提供的正文。`route_policy=oa_first_then_institution` 是协议中的路线次序，不能覆盖 `session_mode` 的权限限制。

获取服务负责合法路由、传输、内容验证、hash、typed 失败和私有 manifest，只写调用方指定目录。已选正文获取始终传 `--no-si` 和 `--cnki-format pdf`，无需再次确认补充材料；DeepFetch 接受核实的 PDF、HTML、XML 正文，CAJ 不适用。

## 持续工作与本机经验

在同一长期 Acquisition Session 和 workspace 中执行后续获取，复用已有工具、有效配置与工作记录。开始新任务时，先用适合本次模式的已验证入口和快捷路径，减少重复找浏览器、查机构入口及探索已走通过程。独立 request 保留各自执行事实，不要求重复探索未变化的机器环境。

把值得复用的本机经验写入 workspace 的普通工作笔记：可用入口、具体操作或命令、适用条件、已知失效点。复用现有配置、缓存、manifest 与路线记录，不另建一套路由记忆服务。记忆只表明过去实际成功或失败；访问条件改变时只修受影响路径，并更新笔记。密码、cookie 和私有认证状态仍留在受控环境，不进入笔记正文。

完成一批或压缩模型上下文后保留这个 workspace。后续继续使用已有文件和必要经验；清理无用临时文件不等于重置获取会话。只需知道当前要下载的论文和本次获取条件，不需要整体研究背景。

## 1. 预检

`oa_only` 跳过全部机构／浏览器预检，并传 `--no-institutional-access`；这是已选主路线，不描述为被迫降级。`provided_only` 仅核实已提供文件，不发起网络下载。初次需要机构路线时检查配置、连接和真实权限；后续任务先复用当前仍有效的配置、健康缓存与受控页面，仅对过期、变化或出错的环节重新探测，不每批机械重跑全部预检。宿主负责主动检索计时，下载 Session 无需了解研究流程。

按需使用以下检查；普通 health 可复用缓存，只有需要强制刷新相关连通性时才加 `--force`：

```bash
python3 scripts/configure_school.py show
python3 scripts/configure_school.py health
node scripts/functional_cdp_probe.mjs --proxy http://127.0.0.1:3456
```

先检查已有受控浏览器页面。显示机构身份的出版商正文页，或核实的付费正文响应才能证明访问权限；图书馆首页和 `/targets` 可达只证明连接。新开登录页不推翻其他已授权页面。

代理 `new`／`eval` 失败而原生 Chrome CDP 可用时，在未占用回环端口启动随包桥接并重新功能探测，所有机构请求传同一 `--proxy`。原生 CDP 已在 9222、预期代理缺失时可复用：

```bash
node scripts/direct_cdp_proxy.mjs --chrome http://127.0.0.1:9222 --port 3456
curl --noproxy '*' --max-time 10 http://127.0.0.1:3456/targets
```

桥接复用现有浏览器，仅暴露页面操作。凭据、cookie、local storage 和用户配置文件留在受控环境，不进入 Agent 上下文。图书馆连接错误或机构访问仍不可用、确需用户协助时，下载 Session 可通过宿主提供的 `human_request.open` 发送请求，说明错误现象、已尝试办法和所需操作（例如连接校园网络／VPN、在原浏览器重新登录或完成验证码）。仅标记 waiting_user 不等于请求已发送，以正式工具回执为准；当前无正式工具上下文时如实说明待办，不能伪称已投递。保留 Session、workspace 与受影响任务，用户处理后接续；改变访问模式仍以本次明确选择为准。纯用户等待不计主动检索时间。完成条件：当前模式已明确，所需访问已核实或有明确等待原因。

图书馆访问求助使用 `library_reconnect`，成功发送后交回宿主等待。用户回复后先用原 effect 经 `human_request.open.reconcile` 对账，再复验连接并继续下载；“已连好”的文字回复本身不等于已恢复访问，也不能把结果未知当成需要重新发送同一请求。

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
