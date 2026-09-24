---
name: target-execution
description: 实施或修订由 Bundle 启用的 Target，完成材料获取、数据整理、湿实验、文字研究或计算，并交接真实产物与评价。
---

# 实施已接纳的 Target

负责本 Target 的实际方法、实施、证据检查和局部修订。以 Owner 上下文的 `execution_contract.measurement_contract` 为精确科研合同，先明确研究问题、方法、输入、证据和完成判据，再选择动作。研究范围取自相关 obligations 与 Briefs；Question 和 Quest 的总体标准留给综合判断。负结果、无定论和如实报告的失败尝试都可能完成一项调查责任，技术阻塞保持其真实状态。

本 Skill 用中文描述执行规则；面向用户的标题、说明、研究正文及回复，按当前根 Session 的 `zh`／`en` 语言偏好直接撰写，委派时传给子智能体。原文、引用、字段和协议标识保持原貌。

## 1. 读取合同和真实输入

按根系统提示的六入口查找与本 Target 相关的已有工作和资源，从冻结输入 manifest 读取实际需要的数据、划分、预测、方法实现与协议。摘要或 hash 不替代原件；缺少材料时指出精确来源和缺口，沿既有输入交接处理。既有结果只按适用范围复用，不因新 Target 或空工作区重复已完成研究。采用 Environment 时沿其来源与使用说明核对当前条件；需要适配或修复时，在已接纳 Target 范围内保存实际工作与原环境关系。

查找方法时用 `research_graph.baselines.page`／`read`，复用前读完整方法。Bundle 的 Baseline 是初始建议；在承诺范围内，按实际工作复用精确 Baseline／Variant（含跨 Baseline），或在正式交接中声明真实新方法。数据版本、上游 Commit、临时路径和研究说明属于本次输入或分析，不改变方法身份；相似方法需人工判断式的明确选择，不自动合并。

完成条件：当前范围、必需输入及其精确版本已明确；缺失条件有具体说明，未被摘要掩盖。

## 2. 实施、观察和修订

在既有授权内开展获取、整理、观察、访谈、档案或定性比较、理论推导、数据构建、计算或湿实验。选择能解决当前疑点的工具与检查；多次尝试、调整和意外观察可以属于同一 Target。Baseline／Variant 表达方法和配方，VariantRun 记录实际实施；ProtocolVersion／Evaluation 表达评价规则，EvaluationAttempt 记录对某个 Run 或其产物的真实评价，MetricResult 保存已声明的观察。实施与评价可以交替，已实施工作也可以等待评价。

资料规模较大时，可把聚焦检索、比较、实施、评价或资产回顾委派给原生子智能体。给出范围、入口和需确认事项，按文件或对象划分并行写入；子智能体返回精确来源、结果、边界和未决项。根 Session 抽查关键原文并负责最终研究判断。候选检索结果还需正式选择，才成为证据或执行输入。

长任务应在观察之间交还控制。用 `agent_runtime.target_run.progress` 和当前 `target_ref` 读取有界输出，后续沿返回 cursor 取增量。按运行阶段、异常、预期有效输出、干预成本和读取开销决定下次观察；`suggested_poll_seconds` 只是备用建议。启动、变更或异常后及时检查，稳定时拉开间隔并推进独立工作。读旧片段时用 `mode=raw`，同时给出 `log_ref`、`stream_ref` 和整数 `before`。完整原始日志仍保留。

运行中即可检查可用数据或 checkpoint，据证据决定继续、调查、暂停、停止当前进程或局部修正。记录观察、决定、实际状态和受影响产物；保留前次尝试。若观察被用于自适应选择参数、checkpoint、停止或路线，在结果中说明其用途，不能再把同一数据当作未接触的最终评估。改变核心科学承诺、Protocol 指标或 held-fixed 条件时，沿正式修订路径处理。

完成条件：实际工作、观察和未解决事项有可读记录；在途命令和未知副作用已对账，或保留明确等待／阻塞状态。

## 3. 保存结果和实际归属

写 `outputs/result.json`，包含 `schema_ref`、`metrics`、`result_disposition` 及忠实表达研究的领域字段。初始 `result_schema` 是表达指导，字段、嵌套和类型可随真实观察演化；Protocol 指标集合、精确身份、来源、合法 JSON 与有限数值检查仍有效。实测零为 `0`，合同允许的未测量值为 `null`，类别、数组和对象保留真实 JSON 值；缺测原因写入结果或 `outputs/analysis`。

出现多个实际 Run、分离或交替评价、无指标报告评价、待评价实施、复用已接纳 Run，或交接可复用 Dataset／Environment 候选时，读取[正式工作交接](references/formal-work.md)。以 `formal_runs` 声明真实生产者、实现版本、输入和产物路径。`evaluations: []` 表示尚未评价；空指标的已执行评价必须有真实评价记录及归属报告。一次工具调用或技术重试本身不构成新的科研 Run。

`implementation/` 保存足以核查实际方法的说明、规程、推导、编码框架或代码。按研究价值、可复核性、复用和存储成本决定每个 Run／评价的保留产物与粒度；checkpoint 可有多个、部分或没有，不限于模型权重。`checkpoint_policy` 的 `required`／`forbidden` 是先前保存建议，不是产物有无的接纳门槛。把重要取舍和局限写入研究说明。

数据获取、版本选择、大型数据或 checkpoint 保存时，读取[数据保存](references/data-preservation.md)，并核对 `execution_contract.artifact_limits`。大型产物沿正常交接保存，体积本身不要求人工导入。仅为具体缺少的权限、来源、资源或必须由人执行的动作请求 HumanRequest，同时继续获授权的独立工作。

日志按真实工作命名。实际训练的完整 stdout／stderr 才写 `logs/train.log` 或 `train-*.log`；实际模型或实验系统评价才写 `logs/eval.log` 或 `eval-*.log`。从进程开始保留原始输出和退出状态，每次重试用独立路径。可用 `logs/audit.log`、`logs/protocol-check.log`、`logs/gate-closure.log`、`logs/verify.log` 保存可用性、来源、准入及结构检查；准备检查只支持其自身事实。没有训练或模型评价时，相应日志保持不存在。

维护 `outputs/analysis/research-note.md`：开头给出短摘要，再说明认识、支持与缺失证据、继续／改向／等待的理由。交接或压缩后续接前更新；沿精确旧版本核查先前边界。当前及上游 `research_notes` 提供摘要和正文入口；历史分页用 `research_memory.research_notes.read` 的 `research_notes_reader` 与 `index_page.next_offset`。精确正文用 `source=research_note_body`、`source_ref=version_ref`；Target 调用省略 `context_pack_ref` 与 `predecessor_ref`。说明不替代正式输入绑定或实际指标。

完成条件：拟保留产物稳定、来源和实际生产者明确，所有想长期使用的材料都已选入交接；临时工作区不承担永久保存。

## 4. 独立审阅并交接

最终交接前委派原生独立子智能体检查完整候选及关键原件，返回自由格式反馈；根 Session 根据反馈和自身判断修订并自查。独立审阅是另一子会话的真实工作，同根自查不能替代；不伪造审阅身份、记录或 Owner 凭据。

Owner 反馈后使用提供的精确合同与拒绝原因，仅修正受影响内容，仅重做被变更失效的工作。保留已接纳版本、真实日志和修订来源。系统接受合法 UTF-8 JSON 并派生规范语义 hash，同时保留原始字节；空白、缩进、转义或末尾换行不要求重跑研究。重复键、非有限数值、非法 schema／外部引用、指标集合或身份来源错误仍需修正。

最终用简洁正文说明证据、局限和未完成工作，由系统构造 completion binding 并经 Owner 接纳。进程结束、provider 回合完成、产物接纳、TargetCommit 和科学结论分别成立。暂停／取消后对账并遵循当前控制状态；恢复沿同一执行及 Owner 事实继续，未知结果先对账原操作。
