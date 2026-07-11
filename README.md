# meta-research 元循环系统 —— 运维操作手册

自主研究编排器：一条命令让真 Codex 全自动跑「出题 → 分解 → 建基线/变体 → 训练 → 评估 → 双评审 →
关问」的研究元循环。**确定性编排器从不推理**（Codex 是无状态阶段工人，只产合 schema 的产物，永不碰
数据库）；SQLite 是唯一真相（36 表冻结 DDL + 三重锁）；普通受信调用由外部 guardian 管理子孙树，
bundle/import 的不可信代码只进入 exact-pinned Docker sandbox；事务边界可从 DB 无半写恢复，执行边界以耐久
receipt 证明本机子树与 daemon container 均排空（适用条件与残余边界见 §7）。

> 设计真相唯一在 `../reference/第一部分-系统架构设计.md`；本 README 是**操作面**（怎么跑、怎么配、怎么
> 观测/干预、边界在哪）。构建历史见仓库根 `ROADMAP.md` / `build_log/`。

## 0. 一分钟跑起来

```bash
cd meta-research
python -m pip install -r requirements-dev.txt          # pytest / jsonschema / PyYAML（+真 Codex 见下）
docker pull docker.io/library/python@sha256:f5cf0344c9886ff24d34797578d5d7dd6e8911ae0fe5962bb55d0f89603ec361
python -m pytest tests/ -q                              # 自验：以当前测试输出为准

# 全自动跑（需真 Codex CLI + 代理，见 §2）：
export HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890
# 生产还须先配置 connectors/outbound.json + 对应 token 环境变量（见 §4.2）
python -m orchestrator.run --system-root . --work-root /path/to/run_dir --max-cycles 50

# 仅离线/测试（明确承认外部通知不会交付）：
python -m orchestrator.run --system-root . --work-root /tmp/canary --max-cycles 5 --no-outbound
```

- `--system-root`：含 `input/goal_brief.md`、`policies/`、`prompts/`、`schemas/` 的仓库根（一般就是 `.`）。
- `--work-root`：本次运行的产物根（`research.sqlite` / `cycles/` / `state/` 落这里）。**重启用同一个
  work-root 即从断点续跑**（DB 权威，非进程内记忆）。同一时刻只能有一个 run owner：入口会先取得稳定
  `.orchestrator-instance.lock` 的非阻塞进程级 lease；随后在 DB、listener、Runner 副作用前恢复/核验
  `state/executions/` 的执行 receipt。第二实例或无法证明已排空的旧执行都会在新外部调用前 fail-closed。
- `--max-cycles`：本次最多推进轮数（安全上限，默认 100，与系统自终止并存）。
- 生产入口默认读取 `<system-root>/connectors/outbound.json`；缺失、权限不安全、token 环境变量缺失或协议
  不合法都会在建库/调用 Codex 前失败。只有显式 `--no-outbound` 才允许离线运行。
- 默认入口在人工 pause 或文件请求阻断时**保持单写进程常驻**，周期消费控制台 spool、扫描提醒，解除阻断后
  自动从同一 DB 游标继续；批处理/调试若希望遇阻即返回，显式加 `--once`。`--poll-interval-s` 可调等待轮询
  间隔（默认 1 秒，最小 0.01 秒）。
- 默认 attack 入口会在打开 SQLite/启动 connector 前核 `/usr/bin/docker`、本地 unix daemon、seccomp 与
  policy 中 exact image digest/image ID；镜像只 inspect、**绝不隐式 pull**。上方 pull 是显式部署步骤。
  syscall 边界不依赖 daemon/platform 默认值：可信 launcher 在 rlimit 后、payload env/代码前加载由 vendored
  Moby profile 生成且 SHA-256 固定的 amd64 BPF；daemon 注入的 seccomp 只作为额外约束。
  当前默认镜像是最小 CPU/Python 3.11 bootstrap runtime；真实项目应先构建含锁定依赖的镜像，再同时更新
  `execution.sandbox.image`/`image_id`，不得改成可漂移 tag。

停机时打印 `[run] dual_mode=… 推进 N 轮：[…]；停因=…`。停因见 §6。

若在 Python 内直接调用 `build_system()`，默认同样强制 lease；必须用 `with build_system(...) as system:`，或在
所有正常/异常分支调用可重试的 `system.close()`。关闭顺序是 listener/pump/delivery、已接纳 query、共享 execution
supervisor（整树排空 + terminal receipt）、只读/写 DB 连接、heartbeat，最后才释放 flock。close 返回错误必须按
停机异常上报；只要仍有能力或无法证明状态的 guardian 未结束，lease 就会保留供再次 close，不能直接另起同
work-root 实例；若所有能力已机械消失，历史 worker 错误可在 lease 释放后返回，仅作故障证据。
`enforce_instance_lease=False` 与自定义 `runner_factory` 只供受信任的隔离测试；后者自行启动的进程不受默认
supervisor 契约保护。

## 1. 启动输入：研究目标书

`input/goal_brief.md` 是启动输入①（§4.6.7）：YAML frontmatter 必含合法 `predicate_json`（成功谓词——
**首次建库时**缺失/非法则**启动即失败**，这是机器契约不是约定；重启已建库时以 DB 里的 goal 为权威、
不重解析 brief，故改 brief 不会污染在建目标——演化走 goal_amend），其后是中文正文。仓库内已有一个 toy
示例（合成二维双高斯二分类）。写你自己的目标时：

```markdown
---
predicate_json: {
  "kind": "metric_comparison",
  "protocol": "<协议名>", "protocol_ver": 1,
  "metric_id": "<指标名>", "metric_ver": 1, "scope": "aggregate",
  "success": { "op": ">=", "value": 0.90 }
}
---
# 研究目标
<中文正文：背景、要回答什么、评估口径注意事项>
```

`predicate_json` 编码「什么算研究成功」——根问题由满足此谓词的真实测量证据关闭；随 `goal_amend` 版本演化。
目标正文引导真 Codex 的出题方向（例：想让它先建基线家族再做变体对照，就在正文里说清）。

## 2. 真 Codex 运行时（工程配置）

真执行走本机 `codex exec` 一次性无状态调用。工程配置走**环境变量**（模型/二进制是工程事实，不进
`policy.yaml` 旋钮注册表）：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `METARESEARCH_CODEX_BIN` | `codex-chatgpt` | 本机已认证的 codex 包装 |
| `METARESEARCH_CODEX_MODEL` | `gpt-5.5` | 模型 |
| `METARESEARCH_CODEX_EFFORT` | `medium` | 推理力度 |
| `METARESEARCH_RUNNER_TIMEOUT_S` | `900` | 单次 Codex 调用超时（工程超时；研究执行时限另见 policy.flow.watchdog） |
| `METARESEARCH_GITHUB_TOKEN` | — | 可选的 GitHub 只读 API token；仅 plan 明确触发 repo discovery/direct resolve 时读取，不落 policy/DB/回执 |
| `HTTP_PROXY`/`HTTPS_PROXY` | — | 真 Codex 需要（本机 7890）；OS 级，由 codex 子进程继承（runner 不显式读） |

冒烟自检（一条命令跑通至少一个完整 attack 轮）：
```bash
python -m orchestrator.run --system-root . --work-root /tmp/smoke --max-cycles 6
```

## 3. 可调旋钮：policy.yaml

`policies/policy.yaml` 是全部可调旋钮的唯一权威（机制代码零硬编码；全量注册表 = 第一部分附录 C）。常改的：

- `budget`：`B0` 单轮预算、`B_max` 上限、`session_max` 全局成本安全网（ledger.money 求和上限）。
- `flow.tau`：自终止判据①——`score_floor` 分数地板、`consecutive_rounds` 连续几轮低分即停。
- `tree_guard`：`max_decompose_depth` / `max_children_per_node` / `max_open_questions`（问题树规模护栏）。
- `execution`：manifest 命令围栏——`default_timeout_s` / `max_timeout_s`；`path_allowlist` 必须与
  `sandbox.readonly_mounts` 完全相同（外部数据根只读映射）。`sandbox` 还固定 engine unix socket、image digest+ID、
  memory/pids/nofile/日志轮转/单文件/输出/CPU/tmpfs 上限；bundle 锚区给出的 runtime `env_hash` 由这些声明机械派生，
  manifest 只能逐字回引。
- `import_materialization`：GitHub commit 物化的 API/archive/file/tree/submodule 大小与数量上限、
  archive redirect host allowlist、LFS 明示策略、adapter 路径和 pinned image 中 CPython 版本/源码
  artifact SHA-256。当前 `lfs_policy=reject`，不会偷偷把 pointer 当模型。
- `session.dual_mode`：A=一 turn 一阶段（默认；A/B 实测定默认属运维）。

> **改 policy.yaml 是决策性变更**（影响研究语义/门禁/预算）——生产改动应走评审。改后 `pytest
> tests/test_schemas.py` 会校验它仍合 `schemas/policy.schema.json`。

## 4. 观测与人工干预（跑起来之后）

- **状态卡**：`<work-root>/state/status_card.json`——每阶段边界原子发布的人可读快照（当前轮/问题/进度）。
- **owner 活性**：`<work-root>/state/orchestrator_heartbeat.json`——原子发布 owner id/PID/状态/序号/时限；
  console 会同时复核稳定 flock、lock metadata generation 与 heartbeat freshness。DB 有在途 cycle 但没有
  `state=running` 的新鲜 owner 时显示 `interrupted`，绝不把耐久游标冒充当前进程正在执行。
- **执行 receipt**：`<work-root>/state/executions/execution-<operation-id>.json`——每次默认 Codex、manifest 或
  harness 执行按 `prepared → running → terminal` 原子发布；普通调用须由 `waitpid(...)=ECHILD` 证明子孙树为空，
  sandbox 调用还须用随机 container name + 私有 label 向 exact unix daemon 证明容器不存在，才会同时写
  `group_drained=true` / `sandbox.container_drained=true`。harness 另在 staging 写 `<log>.process.json` 便利指针，
  权威仍是中央 receipt；日志保持 `.partial → .exit → final` 的提升顺序。
- **控制台指令**：通过 `interaction_message` 入站（连接器落库）→ 分类器保守三分类（可能改状态的一律当
  directive 并回显确认）。`pause`/`resume` 控制推进；`query` 走只读应答器（不改研究状态）。
- **文件请求**：某阶段确实无法自取资料时会产 `resource_request` → 落 `interaction_request(pending)` →
  **系统停在该轮游标处等待**（全局等待，无自动超时，但默认 run 进程仍在消费控制动作）。上传源只允许两类：
  `<work-root>/uploads/<目录>/<item_no>/...`（控制台填 `work/uploads/<目录>`），或
  `<system-root>/input/uploads/<目录>/<item_no>/...`（填 `input/uploads/<目录>`）。解决后 daemon 会校验
  schema/hash/size 并原子接纳到 `<work-root>/input/user_provided/<request_id>/`；该目录是 daemon 托管输出，
  **禁止人工直接写入**。无法提供时可在控制台取消请求并留下理由。
- **审计链**：一切决策/执行/测量都在 DB（`decision` / `run` / `evaluation` / `execution_log` /
  `runner_call` / `ledger`）；产物 transcript 归档在 `<work-root>/cycles/<id>/transcripts/`。

### 4.1 人类控制台（web 查看 + 交互，步⑨）

系统跑起来后，另起一个**独立只读进程**在浏览器里看实时状态、下指令。**单写纪律**：控制台进程 `mode=ro` 读库、
只写入站 spool 文件，**绝不写 DB**——研究真相始终只有 run 进程一个写者。

```bash
# 与 run 并行（同一 work-root）：
python -m orchestrator.console_server --system-root . --work-root <同 run 的 work-root> --port 8765
# 服务会创建 <work-root>/state/.console-capability（0600）并打印其路径。
# 读取其中 64 位 token，在浏览器打开 http://127.0.0.1:8765/#token=<token>
```

页面读取 fragment 后立即清掉地址栏，并只把 capability 留在本标签页的 `sessionStorage`；它不会进入 query、
cookie 或 `localStorage`。全部 `/api/*` 请求都要求 `Authorization: Bearer <token>`。远程使用仍只允许 SSH
tunnel（例如 `ssh -L 8765:127.0.0.1:8765 <host>`），不得把服务直接绑定到局域网地址。
正常模式下，缺 capability、尚未成功取得第一份 `/api/db` 真快照、网络中断、401 或 5xx 都会持续显示
全屏连接保护层并禁用所有 live 控件；只有后续 `/api/db` 成功才解锁。内嵌演示数据只在显式打开
`http://127.0.0.1:8765/?demo=1` 时可操作；demo 模式不发送任何控制 API，不是断线降级模式。

所有控制类 POST（`/api/message`、`/api/directive`、`/api/file-request`）还必须带恰一个
`Idempotency-Key: <32 位小写十六进制>`。浏览器会在 fetch 前先将键写入 `sessionStorage`；网络结果不明或
5xx 时保留并为同一 operation 重用，只有 2xx 回显完全匹配的 `console-<key>` 或确定性 4xx 才清除。
手写 API 客户端必须遵守同样规则，且不得用同一键发送不同 body。

`sessionStorage` 是单标签页/单会话恢复边界：同一标签页并发的完全相同 operation 共用一个键，但多标签页之间
不保证共享；关闭标签页会丢失客户端的未决重试状态。因此不要用多标签页并发操作同一运行；若在结果不明时
关闭了页面，重发前先查看人机审计/spool 回执。单标签页最多保留 64 个未决 operation；达上限后前端 fail closed，
不会淘汰旧键或继续发送。

- **看什么**：顶栏 goal/cycle/预算/心跳 + 九个标签页（问题树 / Baseline 池 / 轮次 / 结论证据 / Import 准入 /
  指令请求 / 决策账 / Goal 策略 / 人机审计）全部来自 `/api/db`（真表投影 + status_card/live/notification/FS）；
  左侧文件浏览器经 `/api/file` 白名单只读（work/ · schemas/ · prompts/ · policies/ · input/）。
- **下指令（入站闭环）**：命令行发命令 → `POST /api/message` 写 `<work>/state/console_inbox.jsonl` →
  run 进程在 **precheck 边界** ingest → 保守分类：`pause`/`resume`（硬指令，回显确认后生效，停/续推进）、
  `query`（只读 grounded 应答，不改研究状态）、`note`（软注解）。**恰一语义**：入站幂等、query 只答一次
  （no-loss/no-dup）。控制台自身不联机推理、绝不直接改状态。
- **显式控件**：待确认硬指令可直接点确认/拒绝；pending 文件请求可在上传后点 resolve，或点 cancel。
  HTTP 进程只把结构化动作耐久追加到 spool；常驻 run 进程校验目标/provenance/文件身份后才迁移权威 DB，
  成功或终态失败都有可审计回执。终态请求的同一 request hash 不会原样重开：阶段重做须消费既有回执，
  或明确改变请求条件。
- **显式演示**：只有 `?demo=1` 启用内嵌 mock 数据，且不发 API；正常页面断线时 fail closed，不会把 mock 当成真状态。

### 4.2 真实双向 connector

通知和 query reply 先按确定性 event_key 写 `<work-root>/state/outbox.jsonl`，并以该 work-root 持久
`producer_id` 构成远端幂等身份，再由独立 delivery 线程投递，
网络慢或失败不会阻塞研究 Runner/interaction pump。支持：

- `webhook_v1`：推荐；接收端必须按 `(producer_id,event_key)` 耐久去重并精确 ACK，可闭合崩溃重放；
- `onebot_v11`：直接向本地 OneBot HTTP API 发送固定 QQ 私聊或群消息；配置强绑定 conversation，
  `producer_id:event_key` 写入正文和 echo。

同一 channel 可配置认证 inbound：严格 webhook HMAC-SHA256，或 OneBot v11 reverse HTTP POST 的
HMAC-SHA1。网络 listener 只写 channel 隔离的 durable spool，run 单写进程再以非破坏 poll/显式 commit
接入 `interaction_message`；只有接纳记录 fsync 后才回 transport ACK。connector/profile/source/conversation/
principal/session/idempotency 均由本地配置派生，provider JSON 不能选择控制域。精确“继续”会在同一事务按
到达时 pause 状态解释，运行中不产生 resume，暂停中才建立待确认 resume。

配置、ACK 协议、OneBot 示例和运行期回执文件见 [connectors/README.md](connectors/README.md)。失败会在
`outbound_delivery_state.json` 保留 attempt/下次重试/消毒错误，成功写 `delivery_receipts.jsonl`；两者均
投影到 web 控制台通知流。query/unclear 事件只回原 channel；直连 OneBot 还会机械校验来源
conversation_id 与固定 target 绑定；directive 生命周期也回原 source connector/conversation。升级前没有
可信来源路由的旧 interaction/directive 事件会记录为 `suppressed`，不会回退到默认 QQ 后误投。

## 5. 系统能做什么（当前研究形态）

- **build target**：从零建 baseline 家族（写代码 → smoke → 代码评审 → 训练 → 出厂评估 → 结果评审 →
  注册入池）。
- **exec target**：既有 legal baseline 上建变体（消融/替换/超参对照）→ register_variant 入池。
- **reasoning-only**：bootstrap 创世根问题 / decompose 分解 / terminate 收口。
- **人机**：query 应答、directive（pause/resume）、文件请求全等待环。
- **安全网**：τ 自终止（价值衰退 / 预算耗尽）；kill-9 崩溃恢复；全自动**不楔死**（任何站不住的 Codex
  产物 → 业务拒/目标 failed + 记账，绝不死循环）。

## 6. 停机语义（停因）

| 停因 | 含义 |
|---|---|
| `score_floor` | τ 判据①：前沿问题最高分连续 N 轮低于地板（价值衰退自终止） |
| `budget_exhausted` | τ 判据②：全局成本（ledger.money 求和）≥ session_max。每次 stage/judge 调用按 `price_per_1k_tokens` 折算；单次越线即落 durable `global_stop` 并阻断后续调用 |
| `cost_accounting_failed` | 预算已开启，但 token 汇总缺失/格式漂移或落账不可信；不把未知冒充真 0，持久停机待人工核账 |
| `prior-terminate` / `max_cycles/terminate` | 上轮选择 terminate / 达 max_cycles |
| `pause 指令生效中` | 人工 pause 阻断 |
| `文件请求 #N 等待用户提供` | 全局等待（阶段发文件请求，待 resolve） |

durable 停机（τ / global_stop DECISION）会落库——下次同 work-root 启动也拒推进，直到状态改变。

## 7. 诚实边界（operational canary）

当前是**强隔离已接线、部署/长跑仍待验收的金丝雀态**，足以让真 Codex 端到端跑通并验证研究链路，但还不能
把“能配置 100 轮”写成“已通过数百轮真实研究验收”：

- 同机同一 work-root 已由稳定非阻塞 flock、owner heartbeat、事务/connector fence、execution guardian 和
  lease-last `System.close()` 机械排斥第二个 `orchestrator.run`。每个 guardian 持一份 delegated flock、作为
  Linux child subreaper 启动独立 session；timeout、取消、直接进程退出但仍有后代、或 owner SIGKILL 时执行
  TERM→KILL 并持续 reap，fsync terminal receipt 后才释放最后一份 fence。普通 Codex/tool-free query 仍走
  `linux-subreaper-session-v1`（可信 host workload）；manifest smoke/train/eval 与 ImportWorker smoke/eval 额外走
  `docker-container-v1`，daemon 容器不是 CLI 子孙，因此 guardian 会在发布 terminal 前独立 inspect exact
  name+label 并 force-remove/复核为空。
- sandbox 只给 host 持久写挂一个隔离 quarantine 输出目录；已验证 fd/tree 私有副本和显式数据根均只读，
  另有有界易失 `/tmp`/`/dev/shm`；network=none、
  rootfs=readonly、PID namespace、non-root uid、cap-drop=ALL、no-new-privileges 与 daemon additive seccomp 在
  `docker create` 后 inspect 反核；launcher 的 pinned default-deny BPF 由 exact Cmd/spec hash 焊死，加载失败即不执行
  payload。输出须在容器排空后通过无 symlink/单链接常规文件/bytes+file-count/hash 闭包，才幂等晋升到
  staging；Docker `json-file` 两段轮转给 stdout/stderr 硬上限，检测到任何轮转会把执行判失败，绝不把截断日志
  当完整测量证据；owner 恰死在 return→publish 缝隙时可从 guardian receipt 续晋升。
- 当前节点 Docker 是 rootless 且明确报告 `CgroupDriver=none`，所以 receipt **只写**
  `resource_mode=rlimit-fallback`，且启动时必须与 policy pin 相同：容器内可信 launcher 设置 hard
  `RLIMIT_AS/NPROC/NOFILE/FSIZE/CORE`，guardian
  另管 wall deadline；绝不声称 aggregate memory/CPU cgroup 已生效。`max_output_mb/max_output_files` 是
  quarantine 后验闸，生产仍必须给 work-root/VEPFS 配硬 byte+inode quota。默认 bootstrap image 也未开放
  GPU/device；GPU 真研究须换成已锁依赖的
  exact image 并在具备受验 device/cgroup delegation 的部署后再宣称资源隔离完成。
- 不可信容器看不到 guardian/provider receipt、SQLite、Codex 凭据或整个 work-root，但同一 host 信任域内的 root/
  orchestrator UID 进程仍能控制 Docker socket 或改本地证据；要防该类 host 对手须独立 service account/VM/远端
  attestation，不能靠 0600/HMAC 自我证明。不同 UID 的 tool-free query 仍要求 guardian 有权排空其树。
- fork child 会丢弃非目标 lease FD；owner 死亡同时由 pipe EOF 与 `PR_SET_PDEATHSIG` 触发 guardian。当前
  work-root 位于共享 VEPFS，同机 owner-kill/fence 可做部署 canary；若生产会跨节点启动，仍须在目标
  VEPFS/挂载参数上做“两节点同时 acquire，仅一方成功”的部署验收，单机测试不能代替它。
- plan 的 `critical/budget_estimate` 已权威落库，critical 失败会确定性早退、非 critical 失败可继续后继；
  动态 `goal_amend` 的专用路由、不可变升版、reasoning/status 按 cycle goal version 隔离与 applicability
  恢复也已闭合。
- 代码物化在编排器管理的 staging（净土物化 + sha256 哈希对账 + argv-only 禁 shell + 路径/env 围栏）。
  manifest/import 执行不再“先按路径 hash、再让子进程重开根路径”：源码树固定已核账的根 dirfd，并在执行
  前后逐 leaf 做 no-follow/hash 闭包复核；checkpoint、外部
  artifact 与用户资产用同一个 `O_NOFOLLOW` fd 经 `/proc/self/fd` + `pass_fds` 消费，并在子进程后重验
  inode/size/hash；checkpoint 登记期间也保持该 capability 与 durable path 绑定。生产 sandbox 会把这些已开 fd/tree
  再复制为私有只读输入快照，因此容器内敌对写者不能利用嵌套目录换位；host 信任域内并发篡改仍按上一条处理。
- 用户文件已有 DB 终态授权、ContextPack ref 白名单、hash/size 复验和稳定只读 fd；非 bundle 阶段只看
  有界 UTF-8 预览。bundle 代码虽已不在 host 裸跑，但这不消除 prompt injection 对研究方向/产物内容的影响；
  schema、独立 judge 与 Gate 只能限制写回契约，不能证明任意第三方文本语义可信。
- Web capability 用于隔离其他本机 OS 用户、跨站浏览器请求和无 token 的本机客户端；同 UID 恶意进程仍可
  读取 0600 token，同源 XSS 也可读取 sessionStorage。轮换时先停 console server，删除
  `<work-root>/state/.console-capability`，再重启并用新 fragment 打开；它不替代 loopback/SSH tunnel 边界。
- 严格 webhook 能让接收网关按 `(producer_id,event_key)` 去重；OneBot v11 标准 API 没有幂等写语义，所以“QQ 已发送但本地
  receipt 前 SIGKILL”可能显示重复消息。不能接受重复时必须用严格 webhook 包住 OneBot，而不是直连。
- **provider 调用与本地成本已可 exactly-once 对账，但仍不是供应商账单**：每个真实 Codex 调用先绑定
  durable `runner_call`；guardian 将 JSON event/stdout 与 stderr 写入 0600 捕获文件，终态回执正常时锚定其
  inode/size/sha256，捕获身份本身失败则显式 `capture_error` 并走未知用量停机。owner 正常返回时立即写
  `provider-invocation-v1`（guardian operation ID、Codex
  thread/session ID〔CLI 有回报时〕、prompt/model/effort、usage 与 execution hash）；若恰在 provider 返回后、
  DB 落账前 `SIGKILL`，启动对账会从 guardian 捕获重建同一回执。ledger 与
  `provider_invocation_accounted` 在一个事务写入，重复恢复只保留一份；token 缺失/两种来源冲突则沿原
  `runner_call` fail-closed，不冒充 0。`ledger.money` 仍只是 CLI 可观测 token × 本地
  `price_per_1k_tokens` 的 policy projection，用于护栏/趋势；receipt 明示无 external invoice，不能宣称与供应商
  最终账单逐分一致。这里保证的是“每个实际 invocation 恰记一次”；若已入账后、阶段业务 phase-commit 前崩溃，
  恢复可能以新的 runner_call 再调用一次，两次会分别留痕计账，并不虚构 provider 侧 exactly-once execution。
- 已支持 build / exec/eval target；默认 `build_system()` 已装配同 owner fenced `ImportWorker`、独立 plan
  answerability reviewer 和 `dependency_wait` 恢复。plan 在类型门确认需引入新外部 baseline 家族且当轮无
  候选时，可产一次受 schema 限制的 `import_search_request.json`；受信 host GitHub REST connector
  在 DB 事务外做只读检索，回执完整后用一个短事务原子登记 pinned commit、搜索快照、冻结
  license evidence/SPDX、机械 auto/review 裁定和完成 marker，然后重渲染 plan 四锚。回执已落、DB 未提交的
  崩溃可不重搜直接续提交；无回执的中断只会重复幂等只读 GET，不会重复登记。
- 三个非默认触发也已接入，但它们不能冒充 `new_structure`：`human_named` 只接受经 hard confirmation 的
  结构化 `inject_question`（规范 GitHub URI + 可选精确 commit + need summary），plan 只能回引其 exact authority
  hash，受信 connector 再固定 revision/license；自由文本 URL 不形成 authority。`stuck` 只有在 policy 的
  visit/consecutive-inconclusive 双阈值命中时才做一次只读普查：visit 是终身防贪心计数，连续失败由
  goal-version scoped append-only decision 独立记账（goal amend 后从零开始）。结果只派生新参照问题，原问题只挂 question
  dependency，永不直接登记候选或 `import_defer`。`sota_reference` 先从 policy host allowlist 做有界 HTTPS
  读取，把 paper/benchmark 原始 bytes 私有写入 SHA-256 内容寻址 blob，再派生独立 baseline-reference 问题；不是按
  `latest` 取隐式事实。两个参照子题在自己的 action-cycle 从父轮 receipt 激活冻结候选，不重复联网。
- 默认 GitHub discovery 候选已能从 40-hex commit 进入真实物化：逐个非 recursive Git tree
  API 对象重算 Git SHA-1，commit archive 不使用 `extract()`，每个 regular file 的 size/mode/blob SHA
  与 tree 交叉核，再记 SHA-256 ledger。同一 commit 的 archive 压缩字节可被 GitHub 重生，因此
  transport SHA 只是审计证据，复现身份用 root tree + 文件 ledger，不会因 gzip 漂移换一个源。
  根仓库和固定 gitlink 子模块的 license evidence path/content SHA 都与同一 commit 文件 ledger 交叉核；
  子模块还独立执行 allowlist 决策。symlink 和 Git LFS pointer 当前 fail-closed。
- 仓库须携 `.meta-research/import-adapter.json` v2：只允许 pinned Python 直接 argv、
  `pinned_image_only` 且无项目级 lock 安装，显式声明 artifact、smoke/eval 和 named factory metrics。
  adapter 通过 sandbox smoke + 代码评审后才能以 repository/name 稳定协议家族 ID 注册；同版本
  scope/指标变了会碰撞并拒绝，不能用换 ID 逃避升版。发布快照在
  `<work-root>/state/import-materializations/{objects,indexes}` 内内容寻址，worker DB 只存有界身份。
  旧候选内嵌 `materialization` v1 仍可恢复。
- 物化实现以 `repository_materializer.py` 作为兼容 facade 与单次编排入口；共享 identity primitives、HTTPS
  transport、Git tree、archive/submodule、adapter compiler、content store 分属
  `repository_materialization_common.py` 与 `repository_materializer_{transport,tree,archive,adapter,store}.py`。
  扩展 LFS、依赖构建或 adapter 生成时应落入对应组件，不得把供应链职责重新堆回 facade。
- 大仓库的 judge prompt 不会整仓读入内存：给出文件数/总 bytes、至多 256 条且合计 32KB 的路径，并按
  adapter/命令入口/Python 优先，在 160KB 总预算（单文件 20KB）内展示内容；其余文件仍受 exact
  manifest hash 闭包约束，但不会冒充已经过语义评审。关键执行路径因截断不可判断时，judge 应 fail-closed。
- 这仍不是“自动复现任意 SOTA repo”：普通仓库缺 adapter 会被拒；LFS OID 下载、项目 lock
  构建/验证的专用 image、adapter 生成+独立评审与部署级 quota/cgroup/device 合同属下一检查点。
- CP11.3c 的 120 轮是无真实 provider 工作负载的控制面/投影回归；尚未完成跨节点 VEPFS 双 owner 实机竞态，
  也尚未运行 100+ 轮含真实 Codex/import/训练与 owner-kill/daemon-loss/预算失败注入的 soak。这两项完成前，
  对“上百轮可用”的结论仍只能是机制上可推进、不是运营验收通过。
- 假执行标记（`source=fake` / `synthetic=true`）是 M0–M3 验收期语义，真执行起已移除。

## 8. 自验

```bash
python -m pytest tests/ -q                       # 全量（含契约/gate/恢复/attack 全链/frozen 锁）
python -m pytest tests/test_frozen_contracts.py  # 冻结件锁：plan.schema + MIGRATION_SHA256 未漂移
```

## 目录布局

```
meta-research/
├── input/goal_brief.md      # 启动输入①：研究目标书（frontmatter predicate_json + 中文正文）
├── policies/policy.yaml     # 全部可调旋钮（唯一权威 = 第一部分附录 C）
├── prompts/                 # system_prompt + idea/plan/bundle/reasoning/judge SKILL（措辞即行为）
├── schemas/                 # 产物 JSON Schema（四阶段 + execution_manifest + review_verdict + policy + sidecar）
├── orchestrator/            # 确定性编排器：run.py 入口 / advancer / attack_stages / gate_* / manifest /
│                            #   harness / stage_provider / compiler / recall / notify / console / mediator …
├── db/migrations/           # 冻结 DDL（0001_appendix_a.sql；三重锁 = checksum + count + user_version）
├── tests/                   # pytest 自验
└── <work-root>/             # 运行期产物（research.sqlite / cycles / state；--work-root 指定，不在仓库内）
```
