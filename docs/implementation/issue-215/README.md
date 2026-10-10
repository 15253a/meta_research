# #215 搜索源交付验收包

此包记录[为 DeepFetch 配置搜索源，支持测试并按 Quest 启用](https://github.com/15253a/meta_research/issues/215)的已完成证据与待验收项。代码有四个有序验证单元；此记录不代表整票验收完成、已合并或已部署。

基准为远端 `v1-test@4bd3796cbb3fbb5f88e7c88ba411695b79dd9695`，基准树为 `c81bc9ea2c2e88c8825786824ec9c9ca8cf4ce9e`。分支为 `codex/issue-215-source-config-20261010`，代码头为 `f1c3c43328931dd35246190605d6f8da9adf0c0f`。

| 单元 | 提交 | 已验证树 |
| --- | --- | --- |
| 直接 MCP 复用 | `58feb87c02d0f31ec29be6f3bd065aedfc4cfb21` | `7be2835626b985441d7d9fceb3e4b1910db8bb85` |
| 来源配置与选择 | `a15fac591421a19771b519e4c90a6572609b564a` | `780db2031c808eea73b2e04cb68b37828afc2851` |
| 共享界面及交付资源 | `71a06608fa64729e595eb966b2957cd724476d26` | `e45314c1e1336745238bd7b3958ea8feafda502b` |
| 运行版本与论文证据 | `f1c3c43328931dd35246190605d6f8da9adf0c0f` | `cdb744b09c438192ddeb6193f8e3cdc2e27e7416` |

## 实现选择

使用 pstack Feature 路线，不使用 Matt 实现路线。[Arena 记录](arena.md)说明方案与代码选择。`SearchSourceRegistry` 统一管理私有来源版本、独立作用域选择、运行清单和宿主回执；MCP 连接、封存与调用复用既有实现。创建文献入口和运行条件共用一个来源组件，分开保存共享配置与当前作用域的允许集合。

网站、Crossref 公共 API、自定义 GET API、直接 MCP 使用明确合同。测试不自动保存、启用或启动研究。DeepFetch 固定整次运行的来源版本，关联各轮实际调用；论文导入核对返回身份、版本和结果性质，保留全部匹配来源。可选图书馆缺省沿既有 OA 路线继续，明确机构访问要求与 `provided_only` 保持原约束。默认 OpenAlex、原生 Web Gate、共同 Acquisition、独立 Reader、RM、确认前快照和 Cycle 职责保持。

## 已完成验证

[units.json](units.json)仅投影最终 Linux 回执的安全字段，保留原件 SHA-256 与本地位置。MCP、配置、运行单元分别收集并完成 20、40、222 项测试，全部通过且零错误或跳过。各单元核对源码封存和锁定的 59 个依赖分发包。数量分别属于不同树，不相加为最终树全量结果。

[frontend.json](frontend.json)记录 32 项 Node、6 项来源浏览器和 14 项既有浏览器用例通过，以及类型检查、实际交付资源构建通过。14 项来自一次 19 通过、1 来源 fixture 失败的组合运行；修正后的 6 项来源运行全部通过，不能把原组合运行记为全通过。保留既有 Vite 大包警告。四单元最终 Comment Sicko 均无待处理项，没有追加通用审查。

## 实际环境与隔离验证的区别

Linux 测试使用冻结源码与锁定依赖的隔离环境；浏览器行为测试拦截 HTTP 请求。两者均不能证明外部来源、原生研究、全文获取或独立阅读成功。

配置单元 `a15fac5` 的实际 Meta 主机 API 验收已完成。最终代码头又在 `native/run-20261009T173849.285294Z` 和 `native/run-20261009T175109.333247Z` 完成两次三类来源配置、测试、保存与重测验收。测试与研究使用分别记录，MCP 测试只做初始化和目录发现。网站的可达或受限结果不当作搜索或全文通过。

root 已在 `run-20261009T173849.285294Z` 实际 UI 只读观察到创建草稿选中 API 与 MCP、未选网站，共享来源与通用 MCP 分区清楚；没有进行 UI 写入。该运行的原生重放遭遇 Windows 环境故障，输出未接纳；拥有的产品进程已停止并直接核实。新干净运行 `run-20261009T175109.333247Z` 继续代表样例。

新运行的原生 t1、t2、t3 已完成并按原始输出导入宿主。[native.json](native.json)投影最终公开摘要与必要回执字段，保留原件位置和 SHA-256。t3 返回 0、输出 schema 有效，3 个公开文件未改写导入。Agent 实际调用 API 与 MCP 搜索源，API 返回记录形成确认出版版本的来源证据。NatureDownloader 获取 ResNet PDF，共 603123 字节，SHA-256 为 `51b5de45eb0b558b19c3affe49503cff50cb170a32de602983d6e2ec286942a7`。

空的可选图书馆配置实际得到 `acquisition_session.mode=oa_only`，`request_count=1`。t3 的补充 Acquisition 输入按原字节及同一 PDF 哈希传入，回执 `native/s/operations/t3/acquisition-input-transfer.json` 记录 `artifact_bytes_rewritten=false`。精确原生父会话的 `reader-spawn-proof.json` 记录真实 Reader spawn；驱动器 `spawn_attempt_count=0` 来自不识别该原生协议的计数器，不能解释为没有 Reader。最终 Reader assignment 为 `complete`，RM 公开快照为 `accepted`，含 1 篇论文、1 份全文；原生 Web 记录 4 次 search、6 次 fetch。

结果为明确受限的 `completion=limited`。OpenAlex 返回 429；不透明 MCP 元数据未确认论文版本，只保留实际调用线索；附录与补充材料覆盖保留受限结论，不声称全部取得或独立阅读。样例使用既有确定性 Companion drafting adapter，未声称真实 Companion 模型执行；DeepFetch、NatureDownloader 与原生独立 Reader 使用实际边界。

人工确认前正式 Quest、Question、foreground Cycle 均为 0。首轮确认 helper 在 6 个回执都接纳后，因立即读取的缓存投影计数未更新而失败；保留失败观察，随后只读恢复，没有重复确认。稳定读回三项计数均为 1，Quest 继承 API 与 MCP，已取消的另一初始化草稿仍仅选网站。通过公开 API 将本 Quest 选择保存为仅 API、revision 2 后，实际 Cua 运行条件 UI 读回 API 选中、MCP 未选。3 个共享源仍均为版本 1，另一草稿选择与已接纳快照均未改变。

此次原生执行使用 Windows Codex CLI `0.162.0-alpha.2`；捕获的 Linux CLI 为 `0.159.0`。平台、MCP 桥接和路径覆盖均显式记录。本轮证据属于 Windows CLI 对 Linux 捕获输入的实际执行与宿主导入，不能作为 Linux CLI 同版本原机执行的证明。原始输入和导入输出保留本地，不复制到此包。

最终 3 个原生 provider 进程与拥有的测试服务进程均直接核实停止，隧道监听数为 0，浏览器已关闭。生产 8768 的 PID、存活状态、启动标识、argv、源码提交、release manifest 与 wheel 哈希逐项未变；未控制生产或退役服务。

原件位于本地 `D:/10-7优化实现8768/.scratch/implementation-215-20261010/`。最终确认回执为 `native/run-20261009T175109.333247Z/confirmation-acceptance-20261009T181637.631510Z.json`，选择读回摘要为同目录的 `final-public-readback-summary.json`，停止记录为 `native/cleanup-final.json`。各原件 SHA-256 见 [native.json](native.json)。真实 UI 观察由 root 的最终 canonical trail 补齐。本包不包含凭据、私有配置、数据库、原始会话、native argv 或 prompt。

## Pending

root 已完成截至原生验收的 49 行决策轨迹真实性自审，记录位于 `trail-audit/accepted-native-before-publication/root-truth-audit.json`，第 16、24、38 行保留追加更正。自审不能替代跨模型家族审查。

- Show Me Your Work 要求的另一模型家族轨迹审查。Claude 实际调用因 403 额度限制未产生结论，没有豁免记录；同 GPT 家族代理不能替代。

提交、PR 与 tracker 发布状态以 GitHub 票据的最终结论及本地交付记录为准。本包不预告尚未发生的发布。

搜狗专项继续暂停，历史 PR #221 保留原结论。#228 的模态布局、Cycle 重构、任意服务自动适配、生产部署和长期科研效果不在此结论中。
