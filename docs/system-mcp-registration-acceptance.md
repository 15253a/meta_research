# 系统 MCP 注册：实现与验收记录

日期：2026-09-26。设计依据：[系统 MCP 注册与根 Session 注入 Spec](system-mcp-registration-spec.md)。

状态：**已于 2026-09-26 19:01（北京时间）部署到 SSH 远端 8769 服务，健康检查与实际页面检查通过。** 注册、公共配置构建、生产 Codex 接线及管理界面已实现；专项验证、完整后端审计及兼容性复核已完成。完整仓库回归未全绿，详见文末的原始结果与基线差分。

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

各轮真实调用的 `thread.started` 均为 `01a0dd2e-de3f-7590-a8f9-0037a2bb3eb3`，没有通过新建原生会话规避配置更新。

| 运行 | 冻结注册修订 | 实际证据 |
| --- | --- | --- |
| `initial` | 3 | stdio `receipt_stdio`、HTTP `receipt_http` 各出现一次 completed MCP 调用，分别返回确定性 receipt；同时配置的不可达 HTTP 服务不妨碍这些调用 |
| `initial` 的图片工具 | 3 | `picture_stdio` 返回原生 `image/png` 内容；模型实际报告左上红、右上绿、左下蓝、右下黄，与无文字提示的 128×128 fixture 相符；不以 base64 或文件路径作为可见性证据 |
| `resume-changed` | 5 | 修改 stdio 工具集并删除 HTTP 注册后，继续原 session；`receipt_new` 调用成功，原生工具目录查询确认旧 `receipt_stdio` 和 `receipt_http` 不存在；仍记得首次给定词 `ORCHID` |
| `resume-scope` | 6 | 将 stdio 范围缩为 target 后继续 companion；实际目录查询返回外部工具列表为空、`receipt_new_available=false`，且仍记得 `ORCHID` |
| `resume-child` | 7 | 恢复 all 范围后，原生父会话创建 `/root/inheritance_check`；子会话实际发现并调用 `receipt_new`，返回确定性 receipt |
| `resume-disabled` | 8 | 停用 stdio 后继续原 Session，当前目录中 `receipt_new` 不存在，且仍记得 `ORCHID` |
| `resume-auth-failure` | 10 | 新增返回 HTTP 401 的服务后，健康 stdio 的 `receipt_new` 仍实际调用成功，原会话正常回复 |

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
| `resume-disabled.jsonl` | `875139cdec5b9b937eca58605046993d4dfed3bf70b765a763f5333678605d1d` |
| `resume-auth-failure.jsonl` | `c095bb37e5e0f8ab2791a5ec320cfea16c3ad69fd97d7db48221d8f5140d269e` |
| `resume-isolated.jsonl` | `9a702ac546b7e81f9624d2b6b3e37a621e59cc16cf23c88ea4b1888724950211` |

HTTP 认证挑战补充验证见 `auth-checks.json`：修订 10 未提供认证引用时检查为 failed；修订 11 配置正确 bearer 环境变量引用后检查为 connected，发现 2 项工具。测试仅使用专用 fixture 凭据。

范围边界：真实实验直接验证了修改、移除、停用、缩小及恢复范围后的原会话工具目录变化，以及 HTTP 401 对健康工具的隔离。未决恢复使用签名 spool/监督进程测试验证，没有在真实设备动作执行中人为崩溃；认证验证不代表覆盖全部外部供应商认证机制。

## 宿主配置隔离的原生复验

锁定版本 Codex 0.156.1 的 `--config mcp_servers={}` 会深度合并宿主配置，单独使用不能清空已有服务。因此生产启动会在应用专属的原生 home 中安装固定名称的 `meta-research-system-mcp-v1.config.toml`，内容为 `mcp_servers=[]` 与 `projects=[]`。随后操作 CLI 层重新提供合法的空 projects 表与完整 MCP 表。前一配置层清除继承的服务和项目信任映射，后一层恢复受控配置；工作目录的 `.codex/config.toml` 不再重新引入外部服务。

这个 profile 只在当前部署管理的 home 创建，不修改 `config.toml`、认证、技能、插件安装或原生 Session 文件。既有同名 profile 内容不符或为符号链接时明确失败，避免静默覆盖。非 MCP 的用户全局配置继续加载；AR 显式能力参数、内部 MCP 的 required 与身份保持原合同。此机制会排除项目目录的原生配置层，研究执行继续由 AR 和操作参数控制。

该版本的 `--strict-config` 会提前校验中间配置层，拒绝用于重置的数组。因此仅具有新系统 MCP 快照的启动路径省略该选项，最终合并结果仍由原生类型校验；注册配置另有严格字段与值校验。历史字段缺失的操作仍使用原 `--strict-config` 和封存 argv，不改变其 hash。版本升级需重新执行这项原生合同验证。

真实无模型回归同时设置用户 MCP、已信任项目 MCP，以及与注册项同名的不同 transport 和嵌套环境参数，证明原生目录只剩选中项且没有继承字段；空注册表得到空目录。随后 `resume-isolated` 在同一个原生 Session 中成功：记得 `ORCHID`，当前目录没有 `user_hidden` / `project_hidden`，健康 `receipt_new` 实际调用成功。证据为 `isolated-native-catalog.json`、`resume-isolated.jsonl` 与原生 journal。

隔离后的子会话也有独立工具调用证据：`01a0dd54-9b9d-7703-89a3-adb83cd9d3b7` 的原生 journal 于 10:48:32.880 UTC 记录 `external_stdio / receipt_new` 的 completed `McpToolCall`，结果为 `MCP_RECEIPT_new_7f392bc1`。

该版本的 app-server 不支持选择命名 profile。独立连接检查在其临时 home 的 `config.toml` 写入相同重置层，再由 CLI 提供最终配置，并以临时目录为工作目录；不会改动生产 home。最终真实 app-server 握手/发现检查成功，发现 2 项工具，fixture 未收到业务 `tools/call`。

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

- 注册与独立连接检查合同：最终 33 项通过，包括无效字段、敏感引用、并发修订、冻结恢复、全部权威根选择、有限超时与进程退出，以及真实原生配置隔离和 app-server 检查。
- 管理 API：2 项公开边界测试通过，包括持久化、范围、修订冲突、既有 CSRF 和非法配置。
- 前端：2 项 Playwright 测试通过，包括桌面滚动、移动布局、CRUD/启停/范围、修订冲突后保留编辑；截图 `web/test-results/system-mcp-desktop.png`、`system-mcp-mobile.png` 已检查。应用及测试 TypeScript 类型检查通过。
- 原生互操作：上述 stdio、HTTP、文本/结构化内容、图片、同 Session 继续、目录移除、认证失败、宿主配置隔离和子会话继承实验通过。
- 覆盖守卫：`test_system_mcp_coverage.py` 的 10 项检查通过。
- Standards 复核：基于 `git diff ebc1cc9`、`CONTEXT.md` 与相关 ADR，对接线/API/UI 及最终历史绑定兼容补丁的复核为 **0 项规范违背、0 项需要处理的结构气味**。兼容桥仅接受捕获的精确类型、instruction hash 和有序源码摘要；仅归一这次升级前后的已审核字段，其余模型、能力、工具、schema、delegate 等绑定仍完整比较，Owner 保留原已接纳身份。8 个当前源码模块摘要已独立核对与 after fixture 一致。
- 最终 MCP 专项合并运行：69 项通过，包含全部 `test_system_mcp*.py` 与 13 项公共 provider/生产 composition 测试，实际原生测试开关已启用。证据 `suite-audit/system-mcp-final-focused.xml`。
- Bundle 策略兼容补充：同一可执行代码的后续文案刷新在应用升级桥之前比较；既有策略、跨升级策略与 Reasoning 兼容合并检查 55 项通过。另 1 项 `bundle_review_result_contract_invalid` 在部署前基线同样失败。证据 `corrected-bundle-policy.xml`。
- 路由测试桩补齐真实 runtime 的 registry/data root/database 依赖，16 项通过；没有在生产代码中加入空注册表兜底。证据 `corrected-web-runtime-fixtures.xml`。

完整后端审计使用 8 个独立 pytest 进程覆盖全部测试模块，并允许收集错误继续；单测试审计上限 90 秒。首次汇总为 **3,013 passed / 230 failed / 19 errors / 2 skipped（3,264 项）**，这是最终修补前的原始审计结果，不能当作全量通过。日志与 XML 位于隔离测试目录 `suite-audit/shard-*`，原始汇总为 `suite-audit/summary.json`。

收集阶段的 9 个缺失旧符号错误已在实际部署基线源码中完整复现。受本次改动影响的失败模块另做精确基线对照，确认旧 Harness、DeepFetch 原生 CLI 可用性、旧 schema/能力预期、旧超时约定和部分 Owner/阶段合同测试已有失败。审计 PATH 漏加虚拟环境 bin 导致的 9 项产品/公开 CLI 用例已单独校正重跑，8 项通过，剩余旧 Claude 后端预期在基线同样失败。已对照复现 105 个原始问题节点，另抽查的 4 项可疑生命周期失败也均在基线复现；未声称所有 249 个原始问题都逐项重跑了基线。分类见 `suite-audit/regression-analysis.json`。不得把上述不同批次的数字简单相加成“最终全量通过”。

子进程封存测试在全量和单独运行中出现超时；以相同 PATH 交替运行基线及最终代码，两者均复现于未修改的 `quest_drafting.run_durable_job` 等待位置。该既有进程收尾问题未在本次 MCP 注册功能中重写；专项未决恢复、历史 argv/hash、Target 与 DeepFetch segment 对账测试通过。

部署前只读核对：远端源码、冻结基线与旧安装包的 282 个包文件逐字节一致；存在与安装内容完全一致的回滚 wheel，数据库 `integrity_check=ok`，未发现正在执行的原生 Codex 进程。部署脚本在写入前再次核对整个包及每个增量目标，并在服务停止后备份 SQLite、核对 11 个 Session/请求表的行数与内容摘要。

## 正式部署结果

- 主机：SSH `172.27.245.212`；服务端口 `8769`。实现提交 `b0b85fff4ff0dc2985c4b36c100ef63c46ebef77`；之后的报告更新不改变运行包。
- 正常停止、安装 wheel、正常启动均成功；部署后服务 PID 为 `1710568`，288 个安装包文件与更新后的远端源码逐字节一致。
- 新 wheel SHA-256：`beb0b5d06bcf9061148f7b5bbc80889c93218d4cb2efc4bd636f72f8756235ff`。完整证据目录为 `/mnt/wl2605001/projects/meta-research-8769/data/logs/system-mcp-20260926/`，包含源码基线/增量、旧安装包备份、SQLite 备份、构建与服务日志、Session/请求表摘要及 `deployment.json`。
- 停止后的安装过程没有改变被核对的 11 个 Session/请求表。服务恢复后的健康检查新增一条认证会话，其余 10 个表的数量与内容摘要仍相同；研究原生 home、签名操作材料、密钥与研究数据库保留。
- 管理 API 返回修订 0、空外部服务列表、9 种根类型及独立检查支持。生产中没有登记验收 fixture。
- 通过现有 SSH 转发实际访问 `http://127.0.0.1:8769/?settings=mcp`，页面标题、系统 MCP 标题、添加按钮和空列表正常，没有浏览器脚本错误；截图 `web/test-results/system-mcp-live.png` 已目视检查。
- 回滚 wheel 为 `/mnt/wl2605001/projects/meta-research-8769/data/logs/handoff-export-layout-20260925/wheels/meta_research_vnext-0.1.0-py3-none-any.whl`，已确认其文件集合及内容完全等于部署前安装包。回滚不得将已继续运行的研究数据库直接覆盖为旧备份。

专项验收及部署已完成。上述完整仓库审计遗留问题作为明确记录保留，本次未扩大为整个仓库的旧合同、迁移测试或进程收尾机制重构。
