# 服务器工作资料引用合同

工作资料引用由 HC 保存原始说明、服务器来源与接收身份。选择或保存引用只证明元数据已保存。它不复制原件、不递归预读、不计算内容 hash、不取得 RM receipt，也不表示理解或研究采用。研究资产页复用选择组件，但仍仅在人的正式登记动作后调用原有 RM intake。

## 选择与接收

浏览器沿现有认证与 CSRF 规则调用以下接口。`server` 来自实际服务主机及运行账户，与浏览器地址或 SSH 转发地址无关。

- `GET /api/v1/server-materials/browse?path=<绝对路径>&limit=50` 返回一层原生顺序的目录页；续页使用原路径及 `cursor`。每页最多 100 项，不递归。目录改变、续页过期或容量已满会返回明确错误。`DELETE /api/v1/server-materials/cursors/{cursor}` 取消续页，需 JSON 正文与 CSRF。
- `GET /api/v1/server-materials/inspect?path=<绝对路径>&description=<原始说明>` 返回完整 `ServerSelection`，含 `server`、`absolute_path`、`kind`、`description`、`observation`、`availability`。stat 数值使用字符串；`observation_ref` 是不透明校验标识。客户端必须封存完整返回值，不重新构造或转换数值。

四类接收身份如下。预览必须整体原样提交，不能由客户端猜测字段或把旧接收位置替换为新工作。

| 接收类型 | 预览与身份 | 明确提交与读回 |
| --- | --- | --- |
| Quest 创建 `creation` | `GET /quest-initializations/{id}/material-receiver`；初始化 ID、草稿 revision/hash、Intent root session | `POST /quest-initializations/{id}/material-references`；初始化 GET 的 `work_materials` |
| 手动 Question `manual` | `GET /manual-question-creations/{context}/material-receiver`；持有的 context、Quest、父 Question、generation，Seed 前可存在 | `POST /manual-question-creations/{context}/material-references`；该 context GET 的 `work_materials` |
| 研究中输入 `current` | `GET /work-materials/receiver?quest_ref=...&question_ref=...`；当前 Quest、Question、Cycle、grant/epoch、真实 roots；Bundle 包含当前可接收的并行 Target | 原 guidance 或 research-input POST 的 `work_materials`；指导 projection 或 `GET /research-inputs/{ref}?quest_ref=...` |
| HumanRequest `request` | 由服务器从请求的 revision、issuer、waiter 与原始 root/work 生成 | 原答复 POST 的 `materials` 加入 `{kind:"server_reference",selection:ServerSelection}`；请求答复的 `delivery.work_materials` 与 `receiving_identity` |

表内相对路径均以 `/api/v1` 开头。前三类提交字段为 `{receiver,selections,description}`，其中 `selections` 为 1–100 个完整选择。guidance 与 research-input 把该对象作为已有正文命令的可选 `work_materials` 字段，正文及引用在同一事务保存。HumanRequest 在既有 pending spool 内保存引用，只有公开答复与运输回执一并 ready 后才可发现；恢复使用相同 reference ID。

首次写入在既有共享事务内核验接收位置。不存在真实可接收 root 时返回 `material_receiver_unavailable`；预览与当前工作不再相同返回 `material_receiver_stale`，不部分提交或替换接收位置。暂停、准备或等待中的工作具有真实 root 时可接收。创建上下文关闭后拒绝新材料。

所有明确提交使用 `Idempotency-Key`。同一 key 和完整相同正文读回已接受结果，先处理 replay，再核验当前工作或原件；后续页面、Cycle 或原件变化不重定向已接受的重试。同一 key 的不同正文冲突。未接受的过期命令需要人明确放弃后重新选择。浏览器刷新保留封存正文及 key。

## 实际发现与读取

`GET /api/v1/work-materials/{reference_ref}` 返回来源、接收身份、可用性、`not_read`/`read`、成功字节范围与失败历史。目录的 `unexpanded` 保留未整体展开事实。成功发现可恢复可用性，但不会把未读材料标成已读。

公开读取接口是 `/work-materials/{reference_ref}/discover` 与 `/work-materials/{reference_ref}/read`。发现参数为安全相对 `path`、`cursor`、`limit`；读取参数为 `path`、发现所得 `observation_ref`、`offset`、`max_bytes`。一次读取最多 65536 字节，返回 UTF-8 或 base64、实际范围、下一 offset 与 EOF。大于旧 64MiB 捕获限制的原件可保存引用并按范围读取。

真实 Root 的 resident MCP catalog 同时提供 `research_workspace.materials.discover` 与 `research_workspace.materials.read`。省略 `reference_ref` 的发现按页列出可见引用；指定引用则展开一层。读取需要 `reference_ref`、相对 `path` 与 `observation_ref`。授权复用实际 RootWorkspaces 可见工作及同 Cycle 谱系，运行 fence 仍由现有验证器核验；引用不会扩大其他 Root 的访问范围。继承 operation-tree 通道的子进程使用相同可调用合同。

创建消费者可用 HC 的 `discover_creation_materials` / `read_creation_material`，传精确 `initialization_id` 或 `context_ref` 及其所属 reference。它们不要求 Seed 或 RM binding。原件使用 no-follow 描述符访问，拒绝符号链接逃逸与特殊文件，读取前后核验来源及字节观察。原件变化需重新发现；缺失或权限失败如实保存。

[#213](https://github.com/15253a/meta_research/issues/213) 接通 Quest／手动 Question 的材料解释与共同工作副本，[#183](https://github.com/15253a/meta_research/issues/183) 接通研究中的消费。此合同交付运输、持久接收身份和可调用 reader，不宣称这些消费者已完成。新引用不得自动适配为全量 RM intake。
