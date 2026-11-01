# #271 普通助手与共用 Writing

实现基线：`v1-test@6a8767e9037eb8166edec89cfc9134618c005ac7`。范围以 [#271](https://github.com/15253a/meta_research/issues/271) 和 [#262](https://github.com/15253a/meta_research/issues/262) 为准。

实现分支：`codex/issue-271-companion-writing-20261101`。代码提交依次为 `fbcd7a8`、`b166eac`、`2b9fa12`；`6566cb4` 仅清理旧原生测试合同，验证文档单独收尾。工作树与现有源码 checkout、服务和研究数据隔离。

## 实际接口

`POST /api/v1/companion/messages` 保留普通消息合同，写作入口额外传 `writing_request`。它包含完整要求、文体、配套演示选择、工作范围、准确所选材料版本、模型配置引用与相对输出目录。HC 在同一现有事务中完成空闲检查、普通 `/new` 和首条消息入队，重试沿原消息幂等回执返回。首轮保留精确科学字段与材料入口，大字段明确标为待读并给出准确 Reader，不截断正文或拒绝合法大材料。后续普通对话和 `/new` 继续使用 Quest 助手目录，不创建 Writing Run、审批或专用工作区。

`GET /api/v1/companion/files` 提供当前助手目录的分页列表与准确哈希下载链接。下载复用目录边界和一次完整读取，文件变化返回冲突，不能将旧链接解释为新版本。它不接纳研究资产、不提交指导、不发布文件。

Companion 的研究目录 grant 只含读取与发现；本身的原生文件制作能力保持。跨页面选择的研究读取使用 `source_quest_ref`，沿目标 Quest 现有、当前有效的 HC 授权核验。用户明确选入写作的模板、前稿等沿现有 RM 导出能力提供准确原生文件，用户选择不会改变研究对象归属。

## 定稿资源

Companion / first_creation 来源为 #256 独立定稿；Writing 来自共享设计 checkout 已合流的 #259 定稿。正式包包含 Companion 方法、first_creation 主文件及三份参考、Writing 主文件及全部五份文体／上下文参考。实际 `research_guidance`、creation adapter 与 Harness 提供准确本地读取路径，研究主链和普通助手使用同一 Writing Skill。参考按任务需要读取，必要资源缺失失败。

真实局部改稿验收发现会话默认语言可能影响英文前稿，迁入后补充一条窄规则：未明确要求切换语言时，沿用所选前稿的语言、风格与格式，修改范围外保持不变。

## 用户 API 内容模型

`assistant.content.generate` 使用服务环境的 `META_RESEARCH_CONTENT_MODELS` 路径读取模型配置。配置文件示例：

```json
{
  "science-model": {
    "endpoint": "https://provider.example/v1/chat/completions",
    "model": "selected-model",
    "api_key_env": "SCIENCE_MODEL_API_KEY"
  }
}
```

当前接通 OpenAI 兼容的 Chat Completions 文本协议。API 密钥由服务环境的命名变量提供，任务只传 `config_ref`。助手显式组织本次所需 `messages`，服务不附加整个目录、历史或 Quest；返回仅含内容及配置引用。调用禁止 HTTP 重定向，避免将凭据转交其他地址。失败返回可识别错误，不回退其他模型。内容仍须由助手核对、修订并制作所需文件。当前模型选项直接由普通 Codex 助手完成工作。

## 清理与验证口径

删除专用 Writing service、adapter、worker、Owner Run / execution / citation adjudication / external delivery、专用 HTTP 路由及前端确认链。研究总览沿 `AcceptedResearchReader` 继续读取原已接纳科研内容。历史数据库迁移文件保留，不添加旧任务运行或转换路径。

验证记录在本工作区 `.scratch/issue-271-20261101/`：完成的 Linux 专项检查有冻结源码压缩包、SHA256、命令、pytest 日志与 JUnit；未完成的全套检查另列实际日志和停止记录。受控 provider／HTTP 测试、真实模型调用及文档渲染分别记录，不能互相替代。实现不代表 8770 已部署，也不代表 #274 的单 Cycle 验收完成。

### 装配与行为证据

| 责任 | 正式入口 | 有区分力的验证 |
| --- | --- | --- |
| 创建准备目录接续、普通会话与 `/new` | HC、`RootWorkspaces`、`CodexCompanionAdapter` | `companion-green04`、`companion-native04`、`companion-proposal05`；持久目录、原生 cwd、首轮／续轮及 manual Seed/Proposal 合同 |
| 跨页面授权读取、讨论与研究写入分离 | `research_content._reading_quest`、`root_operation_catalog` | `authority-final01` 8 passed；真实 HC 确认撤销后读取／发现拒绝、旧续页失效。`reader-final01` 10 passed，直接执行 Question／历史／Baseline／人类输入／资产生命周期返回的 reader，保持准确来源 Quest |
| #256 / #259 正式资源与六角色共用方法 | `creation_basis`、`research_guidance`、原生 provider | `writing-request-01` 31 passed；`test_companion_writing_resources.py` 覆盖六角色 initial/resume；wheel 安装后实际加载 12 份角色／Writing 资源，无旧 Writing runtime |
| 普通 Writing、准确模板／稿件／依赖、下载 | 普通消息 API、`companion_writing`、文件 API | `resources-final02` 25 passed；并发出现新稿仍导出旧准确版本，重复请求不覆盖编辑，下载哈希漂移拒绝 |
| 会话切换与首条 Writing 原子性 | HC 既有 fenced transaction | `companion-atomic-red` 两项先红；`companion-atomic-green` 12 passed，竞争 Writing 被 busy 拒绝，普通消息排在首条后 |
| 大科学内容按需读取 | `ResearchOverviewReader.writing_context` | `writing-science-review01` 22 passed；Owner 接纳的大 Plan 和 Reasoning 可入队、准确 Reader 完整读回，反证／限制保持 |
| 研究内准确产物交接 | 既有 Plan／Target／Bundle 合同与同一 Writing 资源入口 | `research-handoff-final01` 的 Bundle 实际产物来源回归通过；`research-handoff-final02` 的受保护 WorkProduct 与 ScientificOutcome 进入后继 Plan、准备准确 Target 输入回归通过。普通模型成品验收与这些工程合同分别判断，不宣称完成 #274 真实 Cycle |
| 选定外部内容模型与凭据边界 | `assistant.content.generate` | `content-tool-01` 20 passed；最终 `configured-model-http01` 1 passed，以真实 MCP grant 调用本地兼容 HTTP 端点，核对实际请求的 model/messages/凭据、503 无 fallback、研究状态不变；`review-boundaries-01` 的五种真实本地 HTTP 重定向均拒绝。此项不冒充外部供应商模型实跑 |
| 文件分页成本与准确下载 | `RootWorkspaces._discover`／`download_companion_session` | `review-pages-02` 1 passed；只读取当前页，跳过不安全文件后续页不重复；`resources-workspace-idea01` 1 passed，正式根原目录 HumanRequest 交付仍可读 |
| 前端写作、助手文件与投影恢复 | `WritingRequest`、`CompanionFiles`、普通消息 API | TypeScript app/e2e 检查、构建、40 项 Node 测试、11 项 Writing/Companion/MCP 浏览器测试、17 项投影恢复；最终小改后再次构建及 7 项 Writing/Companion 浏览器测试通过 |

`page-cost/receipt.json` 保留同一目录、同一页的实际计数：17 个各 1 MiB 文件，旧实现读取／哈希 17 次、17 MiB，新实现 2 次、2 MiB，返回路径及下一页位置相同。目录遍历在下一条可见路径停止，不展开后续子树；保留无符号链接的读取、文件大小限制与下载时准确哈希复核。目录不是稳定快照，文件变化仍沿既有冲突语义处理；不新增缓存或持久索引。

`final-boundaries-01` 将 Writing request/HTTP/原子性、科学上下文、Companion 与目录边界合并冻结，40 passed。`companion-contract-final01` 两项通过，清理旧英文角色断言，并通过普通原生 provider 的实际 MCP 通道验证不能自行创建研究 HumanRequest。最终 `2b9fa12` wheel 安装加载检查通过，SHA256 为 `e5be974909e0b69075cf7f4a77d82706cec108bcd1ac7af6425f61a3187d3b44`，证据见 `wheel-2b9fa12/installed-receipt.json`。

`companion-native-grant-current02` 1 passed：两个普通原生根交错执行、续轮与同操作重放，保持各自 cwd、文件、work/root 身份；跨根工作目录读取与自行创建研究 HumanRequest 均拒绝。

### 验证限制与独立审查

Standards 与 Spec 分开只读审查后修复了会话竞态、模型重定向、死 `query_commands`、过大首包、整目录重复读取；分页复核又发现并修复跳过不可读文件时的 offset 重复。联合审查进一步修复原授权签发决定与撤销后有效决定的区别，以及返回 Reader 的来源参数延续；只认 HC 当前 `effective_decision`，缺字段拒绝，不回退历史签发决定。以上按各冻结源码 manifest 判断，不把不同源码的测试合称为一次全绿。

完整套件尝试有失败，不能声称全仓通过。`full-01` 因缺少测试环境的 provider-tools 配置中止；`full-02` 使用修正环境，冻结源码 SHA256 为 `3e0de8095efb8333e6aafa23d76a49da22d28767b599091c4ca51a9d3fa500cd`，早于后续专项修复。其 40 分钟上限后记录为 1164 passed、233 failed、2 skipped、3 个已开始但未完成用例，剩余套件未执行完；四分片未形成完整 JUnit。最后一个隔离 pytest 未响应 TERM，核验准确 PID 与专属 basetemp 后停止；清查该验证目录无遗留进程。逐例记录、时限停止和清理事实见 `full-02/observed-results.json`、`deadline-stop.json`、`owned-process-cleanup.json`。

`review-baseline-02` 在原基线源码与原测试复现抽取的全部 7 项失败：创建 dispatching、重评 stale、输出错误码、Harness stdout、Bundle proof、DeepFetch HTTP 409；`companion-recovery-baseline02` 另复现 ACK-loss 夹具失败与旧模拟 Gateway 签名不匹配。未将其余未对照的失败一概归为历史问题。本次修改导致失效的旧 Writing／Companion 写 grant 假设已按现行合同清理，并保留实际拒绝与正式根能力回归。

补验中，旧 WorkProduct 清理场景期望 `removed` 而得到 `skipped`；`research-handoff-baseline01` 在原源码／原测试复现相同断言。保留文件的来源交接场景在 `research-handoff-final02` 通过；未把工作目录回收行为扩大到 #271 改写。

### 与 #270 的组合验证

另建 `integration-270` worktree，分支 `codex/integration-270-271-20261102`，组合代码为 `8d0ad99e37a79410a316e134f28563d667177923`。父链包含 #271 `2b9fa12`、#270 `b0fca9f`，并接入 #270 最终恢复补丁 `19e1be7`。仅 `research_graph.py` 与 `semantic_owner_gateway.py` 有初次合并冲突，解决时保留普通 Literature 与 Writing 清理、只读助手 grant；未修改对方源 worktree。

联合补齐 `research_memory.literature.read` 的 `source_quest_ref` 与有效授权核验，文献页的 metadata、原文和阅读判断 reader 均直接可调用。末轮 `integration-270-271-final01` 六文件 **24 passed、0 skipped、0 failed**，含真实锁定版 vendor CLI 的 strict-exec missing-native 签名证明回归；源码包 SHA256 `050f536e72f9db50192a7c8d6531b2f4aa1e2a974ea71a77f91f97ce289b86bd`。此前联合大组 110 passed、1 项测试夹具形状断言失败，已修并在末轮重验，不计为全绿大组。

之后仅合并 `6566cb4` 的原生测试合同清理，组合 HEAD 为 `a1fc3ef1db2c78c48a7a2dd4aa404528d45483d4`；生产源码不变。`integration-native-final02` 该受影响原生测试 1 passed，源码包 SHA256 `64a57af68992cbcd591b199fde87571dcc9e0bf6fd5e778041eaeec689ec62ff`。

### 真实模型与 Office 文件交付

验收使用隔离 Quest、明确标记的六组模拟数据和仅提供样式的模板，经带认证的正式 Writing 消息入口、`CodexCompanionAdapter` 与真实 Codex CLI `0.159.0` 调用所选 `gpt-6.1-sol`。模型实际读取所选准确材料、执行计算和文件构建，并分别派出原生子 reviewer 检查报告和文章；原生会话的父子关系与工具事件有独立记录。没有以测试脚本生成的模板冒充模型成品，也没有改用其他模型。

报告和短文章各生成两页可编辑 DOCX，以及三页配套可编辑 PPTX。两份演示均保留 28 个可编辑形状与原生表格，没有以截图代替内容。LibreOffice 直接转换实际 Office 字节，逐张查看所有最终 PNG，并以 render-index 绑定源文件哈希。首次渲染发现 `Seed` 表头断行，模型修正列宽后重新渲染；文章 reviewer 发现首页留白及英文用词问题，修订后再次渲染。最终六组数值、失败案例与限制均保留；CSV 对账逐单元格检查数值和派生结果，五个故意损坏的表格样本被拒绝。

| 模型成品 | SHA256 |
| --- | --- |
| 报告 `report.docx` | `22969c25667ff12844b202fd302a9460e732748ea139208f99cea46a9bbd15a5` |
| 报告配套 `report-slides.pptx` | `1d7ab9560c525ee8241e0be40ea967f0bcc2a9efe2f9675ee6d88232566129bb` |
| 局部修订 `report-revised.docx` | `ce3b098cae447742f51ec5bc00d4125cfd73f7f17af94238a6e73cb0c5712997` |
| 短文章 `paper.docx` | `5c0a01cc1fcd781f56d649776bc30c788579d3443bf3d9d37d78df1a54aa7d72` |
| 文章配套 `paper-slides.pptx` | `72da35e24089b925052b594b2e071e1fe0869b842a02a46124028a616079cd12` |

实际 `/new` 改变原生会话而保持同一助手目录，前后七个原文件哈希一致；新会话完成单句局部修订，原报告及演示不变。修订 DOCX 的包级比较确认仅指定正文发生变化，修订后两页也经过实际渲染和模型视觉复核。随后文章入口再新建一次普通会话，八个既有文件（含修订报告）哈希保持一致。文章验收使用最终生产代码 `2b9fa12`，冻结包 SHA256 为 `874a43a80c2a460a6994a5d784dd7c3affc9440528644852c39c70f82d88f79d`；早期报告与修订分别保留其实际源码 manifest，不能视为全部在同一快照上执行。

最终五份 Office 文件直接渲染为十二张页面／幻灯片，验收代理和原生模型均实际查看对应的最终全分辨率 PNG。两个普通材料清单的 56 项文件大小与哈希全部一致，五份 Office 与对应渲染的源文件绑定一致；正式文件接口分页四页，22 次准确哈希下载均返回 200 且逐字节对账一致。

完整记录位于 `real-writing/`：`validation.json` 状态为 `passed_current_codex_real_acceptance`，汇总验收范围；`native-session-evidence.json` 保存真实模型与独立 reviewer 关系，`office-validation.json` / `handoff-validation.json` 保存实际文件结构和目录接续对账，`host-visual-review.md` 与各 render-index 保存逐页视觉和源文件绑定证据，`model-files/` 保存模型成品与复现材料。验收结束后删除本轮隔离环境的两份临时认证副本，两个隔离目录重扫剩余为零；仅本轮反向隧道停止，原登录和代理保留。事实见 `credential-cleanup-evidence.json`、`credential-final-scan.json` 和 `tunnel-cleanup-evidence.json`。

真实外部供应商内容模型未配置，未执行该路径的外部模型验收；上文实际 MCP 到本地兼容 HTTP 的请求与失败测试仅证明工程接线。这里的真实文件验收验证普通助手当前模型，不代替 #274 的完整真实研究 Cycle。
