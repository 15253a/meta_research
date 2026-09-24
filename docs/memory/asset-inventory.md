# 8768 当前资产与写入系统：事实地图

核查日期：2026-09-24。范围为只读源码调查；没有读取运行数据正文、凭据，也没有调用写接口、修改远端源码或运行数据库迁移。本报告提出待讨论决策，不把设计选项视为已获确认的方案。

## 证据基线

- 远端部署根：`<deployment-root>`。
- 调查源码：部署根下 `source/`。已读 `source/CONTEXT.md`；检查部署祖先目录和源码目录，未发现适用 `AGENTS.md`。
- 对 `source/src/meta_research` 与 `release/envs/research-recorder-recovery-utf8-20260924/lib/python3.12/site-packages/meta_research` 执行 `diff -qr --exclude=__pycache__`，无差异。因此下述代码证据对应当前安装包文件；未把安装包一致性等同于完整运行验收。
- 以下代码位置均相对 `source/`，行号为调查时源码行号。未运行测试；已发现资产、Dataset、Finalizer、ResearchNote、Timeline 等专项测试，适合后续实施时选择性使用。

## 当前系统实际分了哪些东西

| 名称 | 当前实现中的含义 | 权威与证据 |
|---|---|---|
| Asset | RM 中可继续追加版本的容器身份；表本身只有 `asset_ref`、`created_at`，没有名称、语义说明或所属 Quest | `migrations/versions/0005_research_assets.py:56` |
| AssetVersion | 一份已接纳的精确内容与 manifest，含 hash、字节数、来源、回执。`memory_ref` 实际等于 `version_ref` | `owners/research_memory.py:267`、`:11354` |
| Asset custody | 精确版本的保管方式：`managed` 为系统受管内容，`linked_local` 为对外部本地文件的精确快照绑定。可将 linked 内容转为 managed | `owners/research_memory.py:3842`、`:4799` |
| Artifact | 多处上下文中的“产物”，没有单一通用 Artifact 根实体。Target completion manifest 保存产物条目，RG 再把精确 AssetVersion 指派到 VariantRun/EvaluationAttempt 的 log/analysis/data 角色 | `target_run_finalizer.py:538`；`formal_entities.py:1050` |
| Bundle | 领域定义是研究实施组织、Target 调度与证据收口。另有 `TargetImplementationBundle`，指确定性实现目录包，二者不能按同一资产集合来理解 | `CONTEXT.md` 的 Bundle 条目；`target_implementation_bundle.py:1`、`:59` |
| Dataset / DatasetVersion | RG 管语义身份与含义，DatasetVersion 绑定一至多个精确 AssetVersion；Dataset 自身不替代字节保管 | `owners/research_datasets.py:85`、`:96` |
| DatasetReference / DatasetDerivation | Question 对精确数据版本的用途引用、精确源版本到派生版本的关系；派生关系记录处理说明且检查环，不复制内容 | `owners/research_datasets.py:121`、`:136` |
| ResearchNote | Target 的自由研究解释；底层是不可变 RM 资产，note 元数据只保留来源和有限摘要，关联 completion/manifest | `research_notes.py:1`、`:18`、`:123`；`target_run_finalizer.py:434` |
| ResearchRecord | 在本次 Python 源码检索中没有找到该名正式领域类型/表。不能假定系统已有统一 ResearchRecord。现有正式事实散布于各 Owner；另有研究记录员生成显示摘要 | 全源码 `ResearchRecord/research_record` 检索；`timeline_summaries.py:1` |
| research-recorder / timeline summary | 单独 `research-recorder/summaries.sqlite3` 保存节点摘要、来源 hash、来源修订与生成任务；设计明确“显示摘要，不是研究事实或工作流前提” | `timeline_summaries.py:1`、`:40`、`:383`；`web.py:773` |
| LiteratureSnapshot | RM 的专门文献快照体系，关联 DeepFetch 精确 request/run/attempt/fence，保存 summary、ledger、fulltext 索引；不是统一 Asset intake 的一条普通文件写入 | `owners/research_memory.py:8563`、`:8646`、`:8777` |

当前应同时区分四个问题：**字节保存在哪里；哪一个精确版本被接纳；它在研究中是什么意思；界面如何帮助阅读它。** 这四者已有不同数据结构承担，并非还未建立任何边界。

## 正式写入口与接纳顺序

### 1. 一般材料导入

`POST /api/v1/research-assets/intakes` → `ResearchMemory.submit_asset_intake` → 持久 intake job → `_prepare_asset` → `_accept_prepared_asset`。

- 请求含来源类型、保管模式、展示名、内容或路径、provenance、可选已有 `asset_ref`；无 `asset_ref` 就创建新 Asset，有则追加该 Asset 的下一版本。文件内容相同可复用物理 hash 对象，但不自动合并语义 Asset 身份。
- 接受 text/file/directory/local_path/repository/link/system_artifact。`link` 保存的是 URL 文本，不是自动抓取 URL 的内容。repository 导入时忽略顶层 `.git`。
- 一般导入本身只证明 RM 接纳了内容；后续独立 `accept_asset_role` 才把版本赋为某 Quest 的 `evidence` 或 `quest_source_material`。
- 同步请求遇 I/O 超时，HTTP 层按相同幂等键回查已持久任务；queued/processing 返回 202。

证据：`web.py:2371`；`owners/research_memory.py:236`、`:3343`、`:3842`、`:4099`；`owners/research_graph.py:10011`。

### 2. 各研究阶段内容

Question、手动 Question、Idea、Plan、Reasoning scientific candidate、自动 Question、Reasoning 都有各自的 Owner 接纳方法，校验各自业务回执与来源后写专门内容事实；在同一个数据库事务中通过 `_insert_managed_content_asset` 镜像进统一 Asset inventory。

这里每份内容事实新建一个 Asset，`asset_ref == version_ref` 且 `version_number == 1`；它们的领域演进靠内容表/阶段关系表达，不是统一 Asset 版本号表达。Implementation content 另有专门接纳事实，运行实际实现目录再走 RM intake。

证据：`owners/research_memory.py:6394`、`:6614`、`:6768`、`:6979`、`:7291`、`:7713`、`:8043`、`:12151`；`owners/target_run_runtime.py:1237`。

### 3. Target 的正式产物收口

运行完成证据 → 冻结 workspace/声明的 artifacts → 每件 artifact 调用 RM intake → 校验精确 binding/hash → 单独事务接纳 completion manifest → RG 的正式研究实体/角色登记 → TargetCommit/Bundle 证据引用。

- Finalizer 的入库幂等键由 completion 和 artifact 快照决定。
- 部分 ResearchNote 路径会查同一 Target 的上一 manifest，把新笔记接到同一 Asset 上，并记录 `predecessor_version_ref`。普通 data/log artifact 没有这条自动版本归并规则。
- VariantRun/EvaluationAttempt 的产物归属优先显式指定路径；仅在生产者唯一且路径符合约定时默认归属。未分配产物保留在 RM manifest，不猜测实验归属。
- 可以通过 Owner 调整 artifact 当前归属；内容版本与原始接纳回执保持不变，另存调整记录支持历史读取和重放。

证据：`target_run_finalizer.py:497`、`:538`、`:635`、`:751`；`formal_entities.py:1016`、`:1050`；`owners/research_graph.py:16525`。

还有两个明确写入途径：Target 实现 workspace 转受管目录资产（`owners/target_run_runtime.py:1237`）；通用执行结果按 result source 逐件接纳为 RM Asset（`:1503`）。Bundle 将已接纳 TargetCommit 的证据文档保存为资产，再赋 evidence 角色（`bundle_stage.py:1771`）。

### 4. 人工输入与输出交付

- 人类回应附带本地材料时，较小材料可以先接纳资产再记录回应；异步路径先保存 HumanResponse 再尝试入队，入队失败会返回 `not_queued`，依赖后续重放补足。它明确允许“回应已保存、材料还未接纳”的中间态。
- Writing deliverable 也走 RM intake，但使用专用幂等 namespace，最终 RM commit 会再次检查当前 Attempt/Fence/授权。该逻辑是报告交付的一种写入口，不应与整个资产写入系统混同。

证据：`web.py:1515`、`:1561`；`writing.py:1030`、`:2724`；`owners/research_memory.py:4090`、`:12079`。

### 5. 文献与显示摘要的独立写入

- DeepFetch → `accept_literature_snapshot` → 文献专用对象与 `rm_literature_snapshots`；正文经 `store_body` 保存，summary/ledger/fulltext 索引使用专门 hash 对象路径。该函数不调用 `_insert_managed_content_asset`。
- Timeline recorder 读取现有事实，更新自己的摘要数据库；不创建正式 RM 资产，不签发研究接纳。

证据：`owners/research_memory.py:8646`、`:8677`、`:8777`、`:9449`；`timeline_summaries.py:1`、`:55`、`:399`。

## 事务、幂等与生命周期已有的保障

- 主数据库启用 SQLite WAL、外键与 FULL synchronous。普通 `write()` 的锁在进程内；敏感接纳处使用 `fenced_write()` 的 `BEGIN IMMEDIATE`。实现基线是一个 daemon 的本地 writer，不能直接当作已验证的多 writer 架构（`database.py:14`、`:62`、`:69`、`:96`）。
- Intake 首先持久化请求 hash 和唯一幂等键；相同键不同请求报冲突，相同键重放读取已有结果。terminal 后清掉请求正文，只保留摘要/hash；不依赖 HTTP 返回成功才算任务存在（`owners/research_memory.py:3343`、`:4224`）。
- 字节写入使用临时文件、hash、fsync、原子 replace；再写 AssetVersion/custody/受管对象登记/intake accepted/feed。数据库部分在同一事务，文件系统与数据库不是一个原子事务（`:3693`、`:4042`、`:6043`、`:6088`）。
- 进程启动把 processing intake 恢复 queued；周期 worker 会回收超过一小时的 processing lease，并对可重试错误重排（`:185`、`:3155`、`:3486`、`:3650`）。
- 版本内容与回执相对稳定，保管状态、可用性、完整性观测单独更新；库存展示可以用最近观测，而实际读取/导出再核验精确内容。
- 生命周期当前提供 managed handoff、hold/release hold、release eligibility assessment。评估要求精确引用修订，检查语义引用、保留 hold、内容可用性/完整性。检查到的 RM 接口中没有正式删除 Asset/对象或执行 GC 的 Owner 方法；release eligibility 是评估，不代表已释放字节（`:965`、`:985`、`:1006`、`:5667`）。

## 需要重画边界的五处问题

以下分清已证实行为与由此推导的风险，没有把静态分析当作已复现故障。

1. **“同一资产”的判据不统一。** 通用 intake 由调用者传不传 `asset_ref` 决定；阶段内容总是新 Asset/version 1；笔记按 Target+路径寻找前驱；Dataset 又有独立语义身份。代码证据是 `_accept_prepared_asset:4099`、`_insert_managed_content_asset:12183`、`_research_note_predecessor:751`。风险是用户所说“同一研究资产的修订、派生、复制、复用”目前可能分别表现为不同机制，检索和界面会被迫猜测。需要首先确定哪些关系属于资产身份，哪些属于研究语义。

2. **“统一资产库存”覆盖范围并不等于“所有研究内容”。** 阶段内容镜像成 Asset，但文献快照采用专门存储；Timeline 摘要也刻意在正式事实之外。证据是 `research_memory.py:12151` 对比 `:8646/:8777`，以及 `timeline_summaries.py:1`。风险是若产品将库存当作全量研究目录，文献/记录容易在检索、导出、引用、保留策略中表现不一致。统一读目录是否需要覆盖这些对象，和是否统一底层写入，应该分开决定。

3. **技术接纳与研究接纳之间存在合法中间态，但缺少统一的产品语义。** 文件可已写到 hash 存储而 DB 接纳失败；一部分 Target artifacts 可已经作为 Asset 接纳而整份 manifest 未完成；HumanResponse 可已保存而材料未入队。证据是 `research_memory.py:3693/:4076`、`target_run_finalizer.py:538/:635`、`web.py:1561`。幂等重试有保障，不能据此说已发生数据损坏；风险是界面或索引把“内容存在”解释为“可引用研究证据”，以及失败后无人管理的未关联对象。需决定何时进入可见、可检索、可复用范围。

4. **谱系已经存在，但没有一种通用谱系模型。** Dataset 有受检查的派生边；Target 有 completion/run/manifest 绑定；note 前驱藏在 provenance；普通 AssetVersion 的 provenance 是可扩展 JSON，而版本表只有顺序号和同 Asset 身份。证据是 `research_datasets.py:136`、`target_run_finalizer.py:557/:751`、`0005_research_assets.py:62`。风险是跨类型回答“这个结论/数据来自哪些精确输入，经过何种处理、由谁接纳”需要专门拼接多个域。需要决定保留按领域解析，还是建立最小通用 source/derived/generated-by 关系层。

5. **留存与回收的业务规则尚未闭合。** 当前有 hold、引用修订与释放资格评估，但本次接口调查未发现正式对象删除/GC 执行器；hash 对象是在 DB commit 前落盘，内容级去重也使“删一个版本”不能等同“删一个文件”。证据是 `research_memory.py:5667/:5785/:6043/:6088`。风险主要是长期空间增长与难以解释保留成本，不是现有证据已被删除。应先确定未接纳/未引用/历史版本的保留期和归档目标，再设计回收闭环。

## 待讨论的决策地图

| 决策 | 用户需要确定的研究含义 | 主要选项与影响 | 依赖 |
|---|---|---|---|
| D1 资产身份 | “同一份资产”按稳定研究用途/内容含义定义，还是由每次正式提交生成独立身份？数据内容调整与含义改变何时分叉？ | 稳定语义身份便于版本浏览，但必须定义改名/合并/分叉；提交身份更简单，相关性由关系层表达 | 先讨论 |
| D2 研究内容目录 | 用户打开资产页，希望看到字节材料、可复用研究对象，还是所有研究记录？ | 分层目录保留不同权威；统一目录可跨类检索，但每条必须明确对象类型和接纳状态 | D1 |
| D3 发布边界 | 已保管但尚未纳入 TargetCommit/领域接纳的材料，能否检索、能否被其他研究正式引用？ | 先保管后发布可保留失败材料；一次业务提交整体可见更简单但需要完整恢复协议 | D1、D2 |
| D4 谱系最小合同 | 用户最常追问的是来源文件、处理步骤、实施运行、研究理由中的哪些？ | 先统一少量精确边+域内细节，或维持领域专用模型并建查询适配层 | D1、D3 |
| D5 保留与归档 | 失败尝试、临时中间产物、旧版本、外部 linked 文件各保留多久，哪些必须永久可复现？ | 全留存易解释但成本增长；分级留存需要引用保护、hold、审核记录和可恢复删除 | D2、D3、D4 |

建议讨论顺序仅为依赖提示：先 D1 → D2 → D3，再 D4/D5。此处没有替用户选择答案，也没有提出立即迁移线上数据。后续实现应依据已确认的决策票，先挑一条完整写入与引用链路验证新边界，再逐步覆盖其他入口。
