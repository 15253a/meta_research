# Meta-Research 真实测试问题台账

> 建立日期：2026-07-14
> 状态：active / append-only
> 用途：记录真实前端验收期间的用户反馈、独立监测发现、复现证据和处置状态。本文档是问题台账，不能替代数据库事实、实验产物或验收证据。

## 测试边界

- 当前允许：本机 Web/控制面交互、quest 创建与切换、只读讲解员问答、队列/数据库/状态一致性检查。
- 当前未获通过：真实 EEG 训练、模型评测、确认性 LODO、sealed holdout 评估。
- DREAMER 是 T1 的 D 阶段 sealed holdout；在 A/B/C、HPO、预处理选择和 claim 形成期间不得读取、探测或试跑。
- 首个真实任务固定为 `T1（开放 · 创新）跨数据集 EEG 通用规律发现`，以任务模板和 claim-lock 工件中的完整约束为准。

## 记录格式

- 严重度：`S0` 数据/科研结论完整性；`S1` 核心流程不可用；`S2` 功能或概念明显不符；`S3` 易用性/表达问题。
- 状态：`open`、`investigating`、`fixed-awaiting-retest`、`closed`、`accepted-risk`。
- 来源：`user`、`monitor`、`system`。
- 每条记录必须包含观察事实、期望语义、复现/证据和后续处置；不把推测写成事实。

## 未解决问题

### RT-001 · 前端缺少新建研究任务入口与任务级隔离

- 日期/来源：2026-07-14 / user
- 严重度/状态：S1 / fixed-awaiting-retest
- 用户反馈：前端需要能够布置新的研究任务；一个研究任务应有一个新的 quest 文件夹，多个任务的 baseline 池和问题树应分别管理。
- 修复前事实：原控制台只绑定单一 work-root/quest，未形成 Web 创建、列出和切换任务的闭环。
- 期望语义：每次新建任务生成独立 quest/work-root、SQLite 状态、baseline 池、问题树和工件命名空间；跨任务不得串读、串写。
- 验收证据：从 Web 连续创建两个任务并切换，证明两边的 baseline、问题树、query/reply 和状态互不出现；重启后注册表仍可恢复。
- 处置：CP12.1 已实现 Web 创建/列出/切换、物理 work-root/SQLite 隔离、持久幂等创建回执；待真实前端复测后关闭。

### RT-002 · 讲解员栏不能回答用户问题

- 日期/来源：2026-07-14 / user
- 严重度/状态：S1 / fixed-awaiting-retest
- 用户反馈：讲解员栏当前不能回答问题。
- 修复前事实：原入口偏向展示/入队，未形成前端提问、后端只读 query、常驻 responder、答案回显的完整闭环。
- 期望语义：讲解员只能解释和查询当前 quest 的真实状态；不得借问答写入科研状态、伪造进度或越权访问其他任务。
- 验收证据：提问后能异步得到有来源锚点的回答；数据库科研状态哈希不因只读问答改变；切换任务后上下文不串线。
- 处置：CP12.2 已实现显式只读 `/api/query` → durable query 分类/应答 → 前端回显，并按 quest 隔离；待真实模型问答复测后关闭。

### RT-003 · T1 尚未达到真实科学实验执行条件

- 日期/来源：2026-07-14 / system
- 严重度/状态：S0 / open
- 观察事实：当前可进行真实控制面/交互验收，但尚无已验收的本机原生 EEG 实验执行后端，也未完成 T1 数据合同、A/B/C 阶段门禁和一次性 D 阶段访问控制的端到端证明。
- 核心概念风险：若把真实 Codex 推理或 UI 状态误称为真实实验结果，会混淆“科研编排已运行”和“数据实验已执行”。
- 期望语义：必须有可审计的数据清单、确定性特征/算子、强 baseline/null、层级统计、LODO、claim-lock 和 sealed DREAMER 一次性评估证据，才能宣称进入对应科学阶段。
- 处置：先完成 T1 bootstrap 与本机执行适配器；按 A → B → C → D 的门禁逐阶段验收。

### RT-004 · 真实测试需要独立持续监测与统一反馈台账

- 日期/来源：2026-07-14 / user
- 严重度/状态：S2 / investigating
- 用户反馈：真实测试期间启动独立子智能体，长期监测后端数据和核心概念是否错位；同时把用户在前端操作时提出的问题记录到 Markdown 文档。
- 期望语义：监测只读且不干扰 owner；发现需包含证据，不以监测者推断改写系统状态；用户后续反馈追加到本文档。
- 处置：本轮启动独立 runtime monitor；主智能体负责把后续用户反馈归档、去重并维护状态。

### RT-005 · 部署后的任务/数据供给仍暴露后端文件操作

- 日期/来源：2026-07-15 / user
- 严重度/状态：S1 / investigating
- 用户反馈：安装到本机后，研究目标、已有数据集目录、全部参考资料目录、初始文件夹以及运行中补交文件都应在 Web 完成；用户不应再返回后端编辑 qualification contract、填写 `source_ref` 或查看后端文件来完成产品流程。
- 修复前事实：旧说明要求用户把文件放到 `work/uploads` 并在控制台填写后端引用；普通 Web quest 也未把初始 corpus 接入真实 Runner cwd，Web 只创建 SQLite，仍需 CLI 启动 owner。T1 数据合同被描述成用户运行前的手工 JSON/路径操作。
- 期望语义：单机版 Web 同时支持浏览器文件/文件夹托管上传与用户明确授权的本机目录只读附件；后端负责路径校验、清单/hash、数据预检、内部合同/密封边界、quest 原子发布和 owner 生命周期。任何失败诊断也在 Web 脱敏显示。
- 验收证据：从空 registry 仅经 Web 提交自定义目标、数据目录和参考目录，发布并启动；Runner 能读取已发布输入；文件请求可在 Web 上传并幂等重放；API 响应不含内部合同/能力路径；owner 失败原因无需打开后端日志即可查看。
- 处置：正在实现 Web draft/分块上传、本机目录附件、自动预检/内部 profile、进程管理与诊断；真实浏览器端到端复测后再转 `fixed-awaiting-retest`。

### RT-006 · 空任务首次打开被“真实快照未取得”遮罩锁死

- 日期/来源：2026-07-15 / user
- 严重度/状态：S1 / fixed-awaiting-retest
- 用户反馈：首次打开前端显示“真实状态快照已过期或尚未取得 / 尚未取得真实快照”，控制动作全部禁用。
- 修复前事实：后端鉴权和 registry 健康，`GET /api/quests` 返回 `200 {"quests":[]}`；前端识别空 registry 后只临时隐藏连接遮罩，但没有 quest 可供 `/api/db` 读取，1 秒 freshness 定时器随即把遮罩重新盖回，导致“新建任务”不可点击。
- 期望语义：空 registry 是合法的产品首次启动状态，应显示由真实 registry 支撑的独立引导页并开放“新建研究任务”；不得把它冒充 quest DB 快照，也不得露出或操作页面内嵌演示数据。首个 quest 建立后仍须等其真实 `/api/db` 快照成功才解锁研究控制。
- 处置：新增零任务全屏引导态、registry 独立 freshness 与并发轮询门；网络/鉴权诊断不再被通用 stale 文案覆盖。浏览器状态机、前端契约与 Web API 相关回归 `83 passed`，当前实时根页已提供修复版本；待用户刷新页面并完成首次新建任务复测后关闭。

### RT-007 · 自定义目标仍显示模板下拉与安全评测字段

- 日期/来源：2026-07-15 / user
- 严重度/状态：S2 / fixed-awaiting-retest
- 用户反馈：新建任务时不清楚“安全评测模式”和“研究模板”的含义；切换到自定义后，下拉菜单仍显示原来的两个模板。
- 修复前事实：后端当前正确返回两个普通预设：真实 EEG/LODO 研究和二维双高斯部署自检，且没有任何密封评测 profile。前端 JS 也正确切换元素的 `hidden` 属性，但 author CSS `.formfield{display:flex}` 覆盖了浏览器的 hidden 显示规则，导致模板、自定义输入和本应完全隐藏的评测字段同时残留。
- 期望语义：自定义是“不使用任何模板、直接填写目标书”，不是第三个模板；切换后模板区必须消失。密封评测不是普通安全开关，只在已部署兼容 T1 one-shot 边界时出现；普通任务无需配置。部署自检必须与真实研究明确区分。
- 处置：增加全局 `[hidden]{display:none!important}` 显隐合同；文案改为“使用预设研究方案 / 从空白自定义”和“密封一次性评测（仅高级 T1）”；模板 API 增加真实研究/系统自检分类与目标摘要。相关前端、Web setup 与入口回归 `23 passed`；待用户刷新后复测模式切换。

## 独立监测记录

> 监测智能体在此节按 `MON-YYYYMMDD-NNN` 追加记录。无异常时仅追加带时间窗的健康摘要，避免刷屏。

### MON-20260714-001 · 控制台与 resident owner 停止，磁盘 heartbeat 仍声称 running

- UTC 时间窗：2026-07-14 12:06:36–12:08:30
- 严重度/状态：S1 / open
- 观察事实：`127.0.0.1:8765` 无监听，本地直连返回 `connection refused`；未找到 console/resident owner 进程。`state/orchestrator_heartbeat.json` 仍记录 `state=running`、`pid=1526500`、`updated_at_unix=1784028344.4104085`，但该 PID 已不存在，文件最后更新为 11:25:44Z，审计时已陈旧约 41 分钟。
- 证据：`ss -ltnp | rg ':8765\\b'`（无输出）；`curl --noproxy '*' --max-time 3 http://127.0.0.1:8765/`（无法连接）；`/proc/1526500`（不存在）；`state/orchestrator_heartbeat.json`。
- 影响/概念对齐：当前 Web 与讲解员不可用。原始 heartbeat 文本不能被当作实时状态；展示层必须结合 deadline、PID 身份和进程存活性推导 `offline/stale`，这正是“真实状态 ≠ 展示文本”的边界。服务停止前后 SQLite 没有显示科研事实损坏。
- 已知 RT 关联：RT-004（监测机制）；尚未归并到其他 RT 条目。
- 处置边界：监测智能体不自行重启；已即时通知主智能体，待主流程恢复服务后对比恢复前后。

### MON-20260714-002 · 停机基线的 SQLite、事件游标与科研状态边界健康

- UTC 时间窗：2026-07-14 12:06:36–12:09:00
- 严重度/状态：健康摘要 / observed
- SQLite：以 `sqlite3 -readonly` 读取 `research.sqlite`，`PRAGMA integrity_check=ok`，`pragma_foreign_key_check` 返回 0 条违例。共 1 个已完成 bootstrap cycle、1 个成功的 `reasoning` runner call 和 1 条对应 ledger；`run/evaluation/evaluation_attempt/metric_result/evidence/build_target/execution_log` 均为 0。因此没有把 Codex 推理伪写成真实实验。
- 事件游标：`state/console_inbox.jsonl` 为 175 bytes，cursor `offset=175`，设备号/节点号与文件一致，cursor anchor 与 inbox 的 SHA-256 `69829d9d…d7970cb` 一致；没有倒退或未消费字节。入站 1 条 query，已分类且有 1 条 reply；outbox 为一条 received + 一条 reply，未见重复 event key。
- 讲解员边界：唯一答复是 `responder_kind=template`且无 `runner_call_id`，文本明示“本消息未产生状态变更”；这证明只读性，但也印证 RT-002 的已知缺口：尚非真实问答。
- Docker/DREAMER：工作目录无 DREAMER/SEED/FACED/DEAP/MPED 数据文件或引用，没有相关活动进程。现有 deployment preflight 记录曾被动读取 Docker 环境事实且标记 `production_ready=false`，但未见 `docker run/exec` 或实验执行证据；这不能作为“本机原生实验已验证”的证据。
- 已知 RT 关联：RT-002、RT-003。

### MON-20260714-003 · 服务恢复后 owner 存活且未误推进科研阶段

- UTC 时间窗：2026-07-14 12:09:51–12:10:21
- 严重度/状态：恢复验证 / healthy
- 服务：`127.0.0.1:8765` 由 PID 1604489 监听，根页 HTTP 200；使用本地 capability 验证 `/api/db` 为 HTTP 200（台账不记录 capability 值）。resident owner 为 PID 1604490，命令明确是 `--max-cycles 0 --no-outbound --poll-interval-s 0.5`。
- heartbeat：owner 已换为 `owner-7ca9…dfc73`，API 推导 `orchestrator_status=active`、`mode=idle`、`orchestrator_heartbeat_age_s=0.962`。两次盘上读取的 sequence 从 103 增至 104，PID 与活动进程一致，不再是陈旧 heartbeat。
- 未误推进：恢复前后 DB mtime 仍为 08:43:44Z，表计数仍是 `cycle=1`、`runner_call=1`、`ledger=1`、`interaction_message=1`、`interaction_reply=1`，且 `run=evaluation=evidence=0`；`PRAGMA integrity_check=ok`。即 resident pump 只维持控制/查询边界，没有创建新 cycle 或伪造实验。
- 游标：inbox 仍为 175 bytes，cursor 仍为 `offset=175`，dev/inode/anchor 继续一致；恢复未重放已消费的 query。
- 已知 RT 关联：RT-004；MON-20260714-001 的“服务不可用”现象已恢复，但 raw heartbeat 陈旧时必须由展示层推导 offline/stale 的概念要求仍保留。

## 用户反馈收件箱

> 用户在真实前端测试中提出的新问题先按原意追加到这里；完成复现后提升为独立 `RT-*` 条目。不得因与既有设计预期冲突而省略。

- 2026-07-14：已归档“新建研究任务/quest 隔离”“讲解员不能回答”以及“独立监测并统一记录”三组反馈，分别见 RT-001、RT-002、RT-004。
- 2026-07-15：已归档“部署后全部任务与本机数据供给必须留在 Web”反馈，见 RT-005。
- 2026-07-15：已归档“空任务首次打开被真实快照遮罩锁死”反馈，见 RT-006。
- 2026-07-15：已归档“自定义目标仍显示模板与安全评测字段”反馈，见 RT-007。

## 关闭记录

> 仅当修复证据和用户/验收复测结果齐备后，将条目状态改为 `closed`；保留原始问题及证据，不删除历史。
