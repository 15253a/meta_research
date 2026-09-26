# 系统 MCP 注册：实现与验收记录

日期：2026-09-26。设计依据：[系统 MCP 注册与根 Session 注入 Spec](system-mcp-registration-spec.md)。

状态：注册、公共配置构建、生产 Codex 接线及管理界面已实现；本文列出的专项验证与历史能力绑定兼容代码复核已通过。**完整后端回归和 8769 正式部署仍待完成，不能把本记录当作部署成功证明。** 后续应在本文末尾补充实际结果。

工作基线为从 8769 当前工作树取得的快照 `ebc1cc9`，包括远端原有未提交改动；不是用旧本地副本覆盖部署。原生验证使用部署所锁定的 **Codex CLI 0.156.1**，在隔离测试目录和专用 fixture 上执行，没有连接真实设备或开展研究任务。

## 已实现的合同

注册表是当前部署数据根下的 `system-mcp.json`。生产 composition 为所有相关 provider 和 Harness 注入同一个 `SystemMcpRegistry` 实例。权威根集合仍为 `root_capabilities.ROOT_AGENT_KINDS`；注册功能没有复制另一套枚举。

保存采用文件锁、修订检查、临时文件写入、文件同步和原子替换。每次成功管理修改增加整个注册表的 `revision`；修改项也记录该修订。格式错误或修订冲突不改变原配置。不存在的初始注册表表示修订 0、空列表；已存在但损坏或无法读取的注册表会使新操作明确失败，不被解释为空列表。

新逻辑 provider 操作读取当前注册配置。完成后继续同一个原生 Session 时，保留 `native_session_ref` 并读取最新配置。未决操作重连、segment 接续和结果对账使用原操作快照；历史 spool 没有系统 MCP 字段时继续保持字段缺失，不补入当前注册项。外部配置不进入长期研究 Run 的可变能力绑定。

外部原生命名空间为 `external_<server_id>`，内部 `meta_research` 名称保留。内部 MCP 的凭据、范围和 required 配置仍由既有机制负责。原生运行器连接外部服务，文本、结构化内容和图像走原生 MCP 通路；注册层没有增加工具调用代理或 MCP 协议客户端。

## 管理入口与配置

工作台及研究现场提供“系统设置”入口，地址为 `/?settings=mcp`。API 使用现有应用认证与 CSRF 机制，基础路径为 `/api/v1/system/mcp`。

| 请求 | 请求体 | 结果 |
| --- | --- | --- |
| `GET /api/v1/system/mcp` | 无 | 注册修订、注册项、权威根类型、装载记录、连接检查记录 |
| `POST /api/v1/system/mcp` | `{"expected_revision": 0, "config": {...}}` | 创建并返回新列表，HTTP 201 |
| `PUT /api/v1/system/mcp/{server_id}` | `{"expected_revision": N, "config": {...}}` | 完整替换该项可编辑配置；启停也使用此接口 |
| `DELETE /api/v1/system/mcp/{server_id}` | `{"expected_revision": N}` | 移除当前注册项，保留已有操作快照 |
| `POST /api/v1/system/mcp/{server_id}/check` | `{"expected_revision": N}` | 检查该项指定修订，返回独立的连接证据 |

修订冲突返回 409，非法配置返回 422，注册存储不可用返回 503。修订号严格要求非负整数。编辑冲突时界面保留已输入内容，让用户重新读取后核对。

HTTP 配置示例中的名称仅为部署环境变量引用：

```json
{
  "server_id": "camera",
  "display_name": "实验室摄像头",
  "description": "只读画面工具",
  "enabled": true,
  "scope": {"mode": "all"},
  "transport": "streamable_http",
  "connection": {
    "url": "https://camera.example.invalid/mcp",
    "http_headers": {"X-Project": "lab"},
    "bearer_token_env_var": "CAMERA_MCP_TOKEN"
  },
  "startup_timeout_sec": 10,
  "tool_timeout_sec": 60
}
```

stdio 示例：

```json
{
  "server_id": "local_fixture",
  "display_name": "本机 MCP",
  "scope": {"mode": "root_kinds", "root_kinds": ["companion", "target"]},
  "transport": "stdio",
  "connection": {
    "command": "/usr/bin/python3",
    "args": ["/srv/mcp/server.py", "--read-only"],
    "cwd": "/srv/mcp",
    "env": {"LOG_LEVEL": "info"},
    "env_vars": ["DEVICE_ACCESS_TOKEN"]
  }
}
```

省略 `enabled` 时为 `true`，省略 `scope` 时为 `all`，省略说明时为空字符串。`all` 动态按权威根集合解释；显式 `root_kinds` 必须非空且没有未知项。stdio 参数始终为数组，不拼接 shell 命令；未填写工作目录时，保存为注册表所在数据根的绝对路径，不随 Target 工作目录变化。命令、路径、URL 中的 localhost 均指执行服务器。

普通环境配置 `env` 与敏感引用 `env_vars` 分离。HTTP 可使用 `bearer_token_env_var`，或 `env_http_headers` 将请求头名称映射到环境变量名称。普通请求头中的认证值、URL 用户信息和已知敏感查询参数会被拒绝；同一请求头的大小写冲突也会被拒绝。外部引用不得指向 `META_RESEARCH_*` 内部环境变量。引用值在执行主机解析，敏感值通过独立环境传输，不写入普通快照、原生 argv、管理返回或提示词。

| 超时 | 默认值 | 可配置范围 |
| --- | --- | --- |
| 连接及工具发现 `startup_timeout_sec` | 10 秒 | 有限数值，1–60 秒 |
| 工具调用 `tool_timeout_sec` | 60 秒 | 有限数值，1–600 秒 |

所有外部服务原生配置 `required=false`。连接检查的 RPC 总预算为该项连接超时加 5 秒，随后有界终止进程树；启动超时为 1 秒的测试验证整体低于 9 秒且进程已退出。检查并发最多 2 个，同一服务不重复启动检查。普通配置合法但服务暂时离线时仍允许保存。

## 根类型及真实调用入口覆盖

以下将生产接线、公开 provider 测试和少量真实原生测试分开记录。`wake`、`successor` 是 AR/阶段编排中的进入原因，不是为每个 provider 增加的独立 CLI 命令；最终新操作经过同一 provider 边界，以有无原生引用选择 initial/resume。恢复由已持久化的操作身份决定，不能仅凭 CLI 的 `resume` 字样判为旧操作恢复。

| 根类型 | 当前真实入口与公共路径 | 本次直接观察的测试覆盖 |
| --- | --- | --- |
| `idea` | `generate_draft`、`review_draft`、`execute`；主调用、复核、HumanRequest/拒绝后续调用；公共 `_invoke`/durable spool | `test_idea_skill_contract.py`：同一原生 Session 的 review/continuation 读取新配置；预启动恢复保留旧 argv/hash；旧字段缺失兼容 |
| `plan` | `generate_draft`、`review_draft`、`execute`；新阶段及后继 Cycle 经相同主调用边界 | `test_plan_skill_adapter.py`：公开 execute 的主调用及复核选择 all/匹配项，排除其他根范围 |
| `bundle` | `generate_draft`、`review_draft`、`execute`；本轮实施与拒绝后的主调用；不改变 Cycle 后继入口规则 | `test_bundle_skill_adapter.py`：完成拒绝后主调用正确选择范围；复核通过共享公共基类接线，未另做一次独立原生模型测试 |
| `reasoning` | `generate_draft`、`review_draft`、`resume_after_autonomous_creation`、`execute` | `test_reasoning_skill_adapter.py`：公开 execute 的主调用及复核选择正确范围，并保留内部 resident MCP；自主创建接续复用公共调用路径 |
| `writing` | 各文档类型的 `generate_draft`、`review_draft`、`execute`，复用 Idea 公共调用基类 | `test_writing_skill_adapter.py`：参数化文档类型的初始/复核调用均装载 writing 范围，并保持同一原生引用 |
| `companion` | `reply`（协作/intent）、`draft`（默认 Quest/Proposal 草拟）；`companion-turn`、`proposal-fork` 的直接及 durable 调用 | `test_external_root_resident_mcp.py`：无内部通道的 pre-Quest reply；`test_quest_drafting_adapters.py`：Proposal fork、默认生产 composition；真实原生继续/子会话见下节 |
| `acquisition` | `acquire` 与 `reconcile` 的根接纳调用，经 `_AcquisitionSkillAdapter`/Idea 基类；reconcile 可形成下一新逻辑操作 | `test_external_root_resident_mcp.py`：公开 acquire 的真实根类型与内部 operation-tree 通道共存；reconcile 接线复用同一公共边界 |
| `target` | `Harness.invoke`、`invoke_terminal`、终态提示恢复；AR 进入原因由 `HarnessInvocation.entry_path` 传递 | `test_system_mcp_execution.py`：同操作冻结、同 Session 下一新操作更新、旧 spool 原 argv/hash 对账、外部凭据消失后已完成结果不重跑 |
| `deepfetch` | `execute` 的 direct/durable；web-evidence-gate 与主调用；同操作多个恢复 segment；reconcile-only | `test_system_mcp_execution.py`：无内部 semantic 通道的 direct；durable segment 共用原快照，下一逻辑调用采样新修订；历史 segment 字段缺失与不重复派发 |

公共注册合同测试遍历实际 `ROOT_AGENT_KINDS`，验证 all、显式范围和 disabled 的选择。各阶段没有各自重新实现命名、校验或编译规则。以上为代表性公开边界测试与源码接线矩阵，**不表示对九种根类型所有编排原因的笛卡尔积逐一运行了真实模型**；实际原生互操作实验使用 `companion` 能力配置，验证共同编译器和锁定运行器合同。

`tests/test_system_mcp_coverage.py` 的 10 项检查将上述矩阵与权威根集合、真实生产 adapter 类、构造注入参数和现有测试名称绑定。新增根类型或删除对应验收测试会要求同步更新矩阵；这个守卫不替代被引用测试本身的行为验证。

## 原生互操作证据

证据存放于隔离测试工作区的 `native-evidence/`，测试服务源码为 `tests/fixtures/system_mcp_server.py`。不提交或展示测试原生 home 中的认证文件。

四轮真实调用的 `thread.started` 均为 `01a0dd2e-de3f-7590-a8f9-0037a2bb3eb3`，不是新建四个原生会话。

| 运行 | 冻结注册修订 | 实际证据 |
| --- | --- | --- |
| `initial` | 3 | stdio `receipt_stdio`、HTTP `receipt_http` 各出现一次 completed MCP 调用，分别返回确定性 receipt；同时配置的不可达 HTTP 服务不妨碍这些调用 |
| `initial` 的图片工具 | 3 | `picture_stdio` 返回原生 `image/png` 内容；模型实际报告左上红、右上绿、左下蓝、右下黄，与无文字提示的 128×128 fixture 相符；不以 base64 或文件路径作为可见性证据 |
| `resume-changed` | 5 | 修改 stdio 工具集并删除 HTTP 注册后，继续原 session；`receipt_new` 调用成功，原生工具目录查询确认旧 `receipt_stdio` 和 `receipt_http` 不存在；仍记得首次给定词 `ORCHID` |
| `resume-scope` | 6 | 将 stdio 范围缩为 target 后继续 companion；实际目录查询返回外部工具列表为空、`receipt_new_available=false`，且仍记得 `ORCHID` |
| `resume-child` | 7 | 恢复 all 范围后，原生父会话创建 `/root/inheritance_check`；子会话实际发现并调用 `receipt_new`，返回确定性 receipt |

子会话证据不仅是父模型总结：原生父 rollout 记录 `SubAgentActivity` started/completed，子原生引用为 `01a0dd30-0692-7cb3-8a4e-20f2b1bd9f41`；子 rollout 于 10:08:35 UTC 记录了 `external_stdio / receipt_new` 的 completed `McpToolCall` 与结构化 receipt。`new.log` 同时记录对应 fixture `tools/call`。紧凑 CLI JSONL 未完整暴露 spawn 细节，因此使用原生 journal 交叉核验。

`stdio.log`、`new.log` 和独立 `fixture.log` 的 initialize/list/call 记录均显示内部会话令牌环境变量不存在；显式引用的 fixture 凭据存在。这里只记录布尔证据，不记录凭据值。该验证证明当前原生 stdio 环境传递没有转交内部 bearer；不是对执行主机上任意恶意程序的操作系统隔离承诺。

独立连接检查调用锁定版本 `app-server` 的 `initialize`、`initialized`、`mcpServerStatus/list(detail=toolsAndAuthOnly)`，不创建 thread、不发送 model turn、也不调用设备业务工具。只在原生返回 `serverInfo` 且无 `toolsError`、含工具发现结果时标记 `connected`。没有握手证据则为 unknown；保存配置不等于连接成功。

独立检查采用临时原生状态目录，避免和正在使用的原生 home 争用；外部连接参数、命令、cwd 和敏感引用仍来自同一编译器及受保护执行环境。检查后终止原生进程树并清理临时目录。实测缺失 stdio 可执行文件返回 failed 用时 1.10 秒；随后健康 fixture 返回 connected、发现 2 项工具用时 1.17 秒，见 `probe-contract-results.json`。请求失败摘要不回传原始 stderr 或服务可能带出的敏感错误文本。

已核对的 CLI 证据文件 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `initial.jsonl` | `ed201c7f47a951b27bacf5107dc7234f64d0e402b9887110af562043a549e772` |
| `resume-changed.jsonl` | `e34c8fcc92b533a00b204ca3b4ed2e1d58a7c85fd4772525cd2fe0423354ef68` |
| `resume-scope.jsonl` | `4e29ce1518dd9d4ce39b6013cc331422dfe8ab6986a4f2791ffe393185eb45d3` |
| `resume-child.jsonl` | `df5ef934174ff9b1df34d724ba4cb8a2a1a6babf4d8b5de53bd5e38948004288` |

范围边界：真实实验直接验证了修改、移除、缩小及恢复范围后的原会话工具目录变化；enabled=false 的选择和持久化由公共合同/UI 测试验证，未额外重复一次真实模型禁用实验。未决恢复使用签名 spool/监督进程测试验证，没有在真实设备动作执行中人为崩溃。HTTP 认证配置编译与错误隔离有合同覆盖，但本次真实 HTTP fixture 未启用认证挑战，不能据此声称已实测所有认证失败模式。

## 状态、快照与运维

| 文件/位置 | 用途 |
| --- | --- |
| `<data-root>/system-mcp.json` | 唯一权威注册配置；只保存普通配置与敏感引用名称 |
| `<data-root>/system-mcp.loads.json` | 最近最多 100 项操作选择记录，含根类型、操作身份、注册修订、snapshot ID 与 server IDs；状态 selected，连接状态 unknown |
| `<data-root>/system-mcp.checks.json` | 最近检查结果，绑定 server ID 及被检查项修订；配置更新后旧检查仍标明旧修订 |
| Idea 公共 provider 的 `provider-operations/.../invocation.json` | 在原签名操作材料中加入 `system_mcp_snapshot`；历史字段缺失保持缺失 |
| Harness 工作区的 `system-mcp-operations/<操作身份哈希>.json` | 签名保存 Target 操作冻结快照；同身份恢复不重读注册表 |
| DeepFetch 的 `deepfetch-initial` 与 `resume-*` invocation | 首 segment 选定配置；同逻辑操作后续 segment 验证并复用它 |

装载记录是配置选择与注入修订的投影，不是所有 Session 连接成功的证明。登记、检查和实际工具调用是不同事实；单独检查成功不能冒充生产各会话均已连接。

首次上线仍需一次正常部署。部署应保留当前数据根、SQLite 数据库、provider homes/native sessions、签名 spool、transport keys 和运行态文件，通过既有服务停止/恢复流程收口运行中的 provider；不能清库、删除原生 Session 或替换成新研究会话来消除兼容问题。部署前记录实际 revision 与运行任务状态，部署后验证健康检查、系统设置/API、旧 Session 可继续和旧操作可对账。正式注册数据初始保持空表，不把验收 fixture 注入生产。

功能上线后，注册管理无需再次部署或重启主服务；新逻辑操作采样最新配置。外部服务安装、启动以及部署侧环境变量变更属于独立运维，注册功能不常驻托管这些服务。停用或移除注册不是取消设备动作的命令。

本期承诺仅覆盖生产 Codex。Claude Harness 和可替换的旧 DraftingAdapter 未纳入系统 MCP 生效承诺；不把管理页面的配置已保存解释为这些后端已接入。OAuth 平台、运行中热插拔、独立子会话注册策略、摄像头/设备实现均不在本次交付范围。

## 验证进度与复核

- 注册与独立连接检查合同：30 项通过，包括无效字段、敏感引用、并发修订、冻结恢复、全部权威根选择、有限超时与进程退出。
- 管理 API：2 项公开边界测试通过，包括持久化、范围、修订冲突、既有 CSRF 和非法配置。
- 前端：2 项 Playwright 测试通过，包括桌面滚动、移动布局、CRUD/启停/范围、修订冲突后保留编辑；截图 `web/test-results/system-mcp-desktop.png`、`system-mcp-mobile.png` 已检查。应用及测试 TypeScript 类型检查通过。
- 原生互操作：上述 stdio、HTTP、文本/结构化内容、图片、同 Session 继续、目录移除和子会话继承实验通过。全套后端测试最终数量尚未汇总，不把不同批次通过数相加。
- 覆盖守卫：`test_system_mcp_coverage.py` 的 10 项检查通过。
- Standards 复核：基于 `git diff ebc1cc9`、`CONTEXT.md` 与相关 ADR，对接线/API/UI 及最终历史绑定兼容补丁的复核为 **0 项规范违背、0 项需要处理的结构气味**。兼容桥仅接受捕获的精确类型、instruction hash 和有序源码摘要；仅归一这次升级前后的已审核字段，其余模型、能力、工具、schema、delegate 等绑定仍完整比较，Owner 保留原已接纳身份。8 个当前源码模块摘要已独立核对与 after fixture 一致。
- 最终待补：完整后端测试结果、正式部署时间/版本、部署后健康与历史 Session 验证结果。
