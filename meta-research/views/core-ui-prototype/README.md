# vNext 核心主界面视觉原型

> THROWAWAY PROTOTYPE — 只用于选择配色、布局与信息密度，不是生产前端。

本页用同一组合成研究数据比较三套结构与气质明显不同的高保真主界面方向：

- `A — 光谱台`：明亮、柔和、留白充分；返场摘要居中，Companion 常驻右侧；包含首页问题路径缩略图、独立问题树视图、`create_question`、四类 HumanRequest，以及实验启动时自动出现的 stdout / 硬件观测窗。
- `B — 夜航室`：深色沉浸式研究空间；用三件关键变化解释系统为何走到现在。
- `C — 研究工作室`：克制的编辑网格；Question、Stage 和三层事实以清晰文字层级呈现。

## 运行

在本 worktree 根目录执行：

```bash
python3 -m http.server 4173 --directory meta-research/views/core-ui-prototype
```

然后打开：

- <http://127.0.0.1:4173/?variant=A>
- <http://127.0.0.1:4173/?variant=A&view=questions&node=Q-38.2>
- <http://127.0.0.1:4173/?variant=A&panel=create-quest>
- <http://127.0.0.1:4173/?variant=A&panel=create-quest&questfetch=running>
- <http://127.0.0.1:4173/?variant=A&panel=create-question>
- <http://127.0.0.1:4173/?variant=A&panel=human-request>
- <http://127.0.0.1:4173/?variant=A&panel=external-request>
- <http://127.0.0.1:4173/?variant=A&panel=offline-operation>
- <http://127.0.0.1:4173/?variant=A&panel=permission-request>
- <http://127.0.0.1:4173/?variant=A&scenario=global>
- <http://127.0.0.1:4173/?variant=B>
- <http://127.0.0.1:4173/?variant=C>

页面底部切换器与键盘 `←` / `→` 也可切换方案。顶部可切换“返场 / 局部等待 / 全局阻塞 / 结果陈旧”四种代表性场景。

A 的左侧“树”入口和首页“打开完整问题树”会进入同一个空间画布：点阵背景、曲线父子边、拖拽平移、滚轮缩放、适配画布与小地图沿用上一版研究控制台的空间操作方式，但不沿用旧版 Answer 状态语义。选择节点只改变浏览器内存中的查看上下文，并同步右侧 Companion，不会修改问题拓扑。桌面端悬停或键盘聚焦节点时，左侧显示 `×` 剪枝入口，右侧显示 `＋` 子问题入口；触屏端两者常显。节点上的 `＋` 打开后续问题专用的 `CreationSeed`，`×` 只打开剪枝影响草案；两者都不会直接写入权威问题树。主导航底部另有 `＋` 创建新 Quest 入口，明确与树内的后续问题创建分开。

首次创建 Quest 使用一个连续表单，而不是“先确认 Quest，再弹出 create_question”。左侧先收集 Quest 目标、完成标准和关键配置。计算资源不再用抽象的“资源范围”下拉框：用户先点击“检测本机计算卡”，查看基于本机 `nvidia-smi` 快照的型号、显存与可用性，再明确选择允许该 Quest 使用的 GPU。原型严格区分 Provider 当前 availability、HC 保存的 Quest Resource Envelope 与具体 Run 才产生的 ResourceBinding；卡片选择不会伪装成 GPU 已预留或某个 Run 已绑定。当前 synthetic fixture 使用本机探测到的四张 A100 80 GiB 演示。

文献配置的主选项改为“全面搜索（包括图书馆）／只搜索开放获取资源／只使用我提供的材料”。选择全面搜索时会出现 `LibraryResourceEntry` 与准备提醒：用户必须先安装 Google Chrome 官方连接器（浏览器插件）、开启浏览器控制，并在同一 Chrome 登录图书馆页面；随后检测连接器、登录会话与入口路径。链接未填、未检测或检测失败都不会阻止／回滚 Quest 创建；用户可以修复环境、改为只搜索开放获取，或直接依据目标生成首问题。原型检测是明确标注的 synthetic preflight，不发起真实网络请求，也不读取或保存密码、Cookie、token、OTP、localStorage 或 browser profile。右侧的 pre-Quest Intent Drafting Session 从打开窗口开始常驻，可以询问配置、讨论 DeepFetch 或要求调整第一问；聊天不会提交左侧表单。首问题的六字段 `QuestionProposal` 由系统先起草，再由用户修改。

生成首问题有两条清楚的路线：默认可不运行 DeepFetch，直接依据 Quest 目标、配置和已有材料生成；也可先运行一次 DeepFetch，界面在选择处明确提示真实任务通常约占用 30 分钟，并显示持续进度。原型用加速 synthetic fixture 演示，完成后自动生成首问题草案。生成结果不是只读摘要，而是与后续问题窗口同构的六字段 `QuestionProposal`：`title`、`unknown_statement`、`answer_shape`、`applicability_scope` 为必填，`background_context`、`requirements_constraints` 可选。用户可以逐项修改或明确重新生成并覆盖；修改 Quest 目标、配置或材料后，已有问题不会消失，而会提示重新复核。两条路线都留在同一个窗口，最后只有一个“确认创建 Quest 与第一个问题”按钮；只有该按钮会同时冻结精确 Quest 草案与当时可见的精确首问题草案，但不会把用户确认伪装成 RG 已创建 Quest、已接纳 Formal Question 或已推进 Stage。此处是首次创建专用交互；问题树里的后续节点继续使用独立的后续问题流程。

后续 `create_question` 窗口仍先收集由人拥有的 `CreationSeed`。原型把“用自己的话描述你想研究什么”设为唯一必填入口；没有这段自然语言就不能确认 Seed。它是用户原始研究意图，不是第七个 `FormalQuestionContent` 字段，也不会被静默当成 `unknown_statement`。Seed 阶段的六个语义字段仍全部选填，材料和 DeepFetch 选择也可稍后整理；用户可以只写一句话，也可以再提供若干文件、一个文件夹或本地目录路径。文件夹用于表达一次待筛选的材料提交；本地目录默认标为 `linked_local` 草案，不暗示已经复制、逐项接纳或形成 RM receipt。用户同时独立选择“使用 DeepFetch / 先不使用 / 稍后决定”；Seed 确认只冻结这项偏好，不直接启动 DeepFetch，也不会提前产生 waiver。确认后，右侧事实栏自动切换为 Question Drafting Session 并聚焦输入框；冻结的 Seed 仍留在左侧供对照，会话仅保存在浏览器内存，可用于把自然语言意图整理成可修改的 `QuestionProposal`。此时右下角的 Seed 按钮替换为独立的“确认最终问题”：桌面端继续编辑左侧六字段，移动端可在“编辑问题草案 / 返回讨论”之间切换；四个必填字段齐全且 DeepFetch 已完成或明确跳过时，最终确认才启用。该动作只确认精确 `QuestionProposal`，不会伪装成 RM/RG 已接纳。选择使用 DeepFetch 时，会话上方先显示等待状态；单独的“开始补充检索”原型交互才启动 synthetic `DF-CQ09`。运行中以一条紧凑检索跑道展示当前阶段、已处理数／本批总数、耗时与最近活动；只有总量已知才显示确定进度，完成也只表示当前获取批次结束。可用 `?variant=A&panel=create-question&deepfetch=running` 直接查看运行态。会话消息不会回写已确认 Seed，也不会提交 Proposal。确认 Seed、确认 Proposal 或 DeepFetch 批次完成都不代表 Formal Question、RM 接纳、RG 接纳或 Stage 推进已经发生。

DeepFetch HumanRequest 窗口绑定精确的 `RequestId / revision`、`DeepFetchRun` 与获取项 fixture。它假设用户已选择 `oa_then_institution`：OA 检索没有合法全文，机构访问随后失效。该 fixture 的 `human_wait_scope=local`，因此只在主界面显示提示，不会自动抢焦点；用户点击提示后才打开窗口。左侧正常路径只给出“我已重连 / 跳过，之后只用 OA / 手动上传该文献”三个按钮；OA 与上传尚未提交时，这三个按钮保持可用并可直接切换路线。右侧常驻的是与表单关联的 Intent Drafting Session：它可以询问当前状态、解释请求或讨论替代方案，但聊天本身不会提交 HumanRequest。备注是左侧独立的可选表单字段。手动材料可选择文件、填写本地文件或目录路径，也可全部留空后直接提交响应。底层 fixture 仍区分 HC response、发起 Owner 的 Evaluation / Disposition，以及 ACQ-17 的 currentness / Resume Validation；Authorization receipt 或 RM Asset receipt 不会冒充“机构访问已恢复”。终态 HR-27 再次打开时只显示只读历史，不会复活同一个 RequestId。`scenario=waiting` 可演示测试失败后以 `needs_input` 保持 active；默认场景测试通过后保留结果窗口，由用户自行关闭。

HumanRequest 的 UI 只保留四类：物理／湿实验与线下操作、外部数据集／API／机构材料申请、图书馆链接重连、低频权限确认。没有第五个“人类任务中心”弹层；主界面的 `!` 与关注卡始终打开当前请求。`human_wait_scope=local` 时只显示关注卡，不自动打断；`human_wait_scope=quest` 且没有安全、有意义的可运行工作时，精确 request revision 在浏览器 session 内自动打开一次，关闭后主界面仍保留全局阻塞提示。`scenario=global` 使用线下实验 fixture 演示这条规则，并优先于 stdout 自动弹层。

四类请求都采用同一个双栏骨架：左侧是最短步骤与响应表单，右侧是全程常驻的 Intent Drafting Session。右侧是完整的会话，不是表单留言框；用户可以询问当前状态、要求解释、讨论替代路线或帮助整理草案。会话在填写表单时继续保留，但不会自行提交左侧内容。

外部数据集／API／机构访问申请只展示三步申请路径，以及可选的申请编号、授权范围、批准凭证与备注；凭证可选文件，也可填写本地材料目录。每张响应表单都把备注突出为“只填这里也可以”的替代路径：用户有不同想法时，无需完成标准字段即可直接提交；DeepFetch 请求也提供独立的“提交这条想法”入口。物理实验／线下操作提供可下载的 [`sensor-calibration-protocol.md`](sensor-calibration-protocol.md)，页面只保留三步摘要；勾选项、设备信息、结果文件／路径、异常说明和备注均可留空。权限确认也允许不选择决定、不填写备注而直接提交。三类表单提交后先显示“回应已提交”，随后退出表单；字段不足只意味着 Owner 之后可能返回 `needs_input`，不应在 UI 层阻止响应提交。补充合同、Owner 和 receipt 信息统一折叠在详情内，避免压过“现在要做什么”。

A 的代表性实验 `RUN-204 / attempt generation 2 / root Session SES-7` 处于 running 且 Execution Fence current 时，新 fence 会自动弹出一次黑色运行观测窗；同一 fence 按浏览器 session 去重，关闭后可从首页“当前实验”卡或问题树工具栏再次打开。若用户正在输入或审查 Command Draft，新 fence 只给非模态提示，不抢焦点或丢失草案。窗口每秒更新 synthetic GPU / VRAM / power / CPU fixture，并持续追加 synthetic stdout，用于验证实时信息的层级、密度、关闭与重开体验；它没有连接真实 Harness 或硬件。

## 边界

- 全部研究与运行数据均为 fixture，只在浏览器内存中变化；`sessionStorage` 仅记住本 session 已展示／关闭的 Execution Fence，以及已自动展示的 quest-scope HumanRequest UI 偏好。`coreWrites = 0`、`networkWrites = 0`。
- 页面只表达 Snapshot / Projection 与 Human Collaboration 交互，不拥有任何领域状态。
- 各 HumanRequest 的 Intent Drafting Session 只保存浏览器内会话示意；它不等于表单备注、HumanRequestResponse、拒绝、授权、MaterialSubmission 或请求处置，也不会静默修改左侧表单。
- Question Drafting Session 同样只保存浏览器内会话示意；它绑定已确认的 CreationSeed，但不能修改该 Seed、确认或提交 QuestionProposal，也不能创建 Formal Question。
- 表单备注及其余字段全部可选；空表单仍可形成 `provided` 响应，业务充分性由请求 Owner 独立判断。
- 主界面和弹窗默认只展示当前任务、必要原因、最短步骤与必须交回的内容；技术身份、详细约束和 receipt 链放入可展开详情或独立信息入口。
- Companion 生命周期与 Writing 精确合同仍在开放票据中；对应位置只用于判断布局，均标记为 `PROTOTYPE ASSUMPTION`。
- 问题分解关系是代表性 fixture；原型只验证树的位置、密度与导航，不提前定义 Answer / Evidence 状态族或生产图查询合同。
- 首次 Quest、后续 `create_question`、图书馆恢复、外部申请、线下操作与权限确认中的身份、内容、步骤和合同标签都是代表性 fixture；这些窗口只验证交互层级与视觉语言，不创建真实 Quest、Question、HumanRequest、Authorization、AssetVersion、实验接纳或恢复许可。
- stdout、Run / Attempt / root Session 身份与硬件遥测均是只读 Runtime 执行观察；只有 current Execution Fence 显示 `LIVE`，旧 fence / 断流会降级为 historical 或 stale。它们不表示 Target 已接纳、Question 已回答或 Stage 已推进。
- 硬件 fixture 明示 collector、device、scope、sampledAt、cadence、unit / denominator 与 freshness；GPU / VRAM / power 为整卡相关值，CPU 为 host-wide 相关值，不冒充某个 Run 的独占用量。生产遥测 transport 仍未在此原型中定义，原始秘密也不得进入日志。
- System Steward 不在此原型中。
