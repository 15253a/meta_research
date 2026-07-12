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
# 默认 policy.deployment.mode=development；这能真跑研究链，但不会产生 production-ready 声明。
# production 还须配置只读 deployment attestation、connectors 与对应 token（见 §3.1 / §4.2）。
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
- 同一启动边界还会把 service、mount、Docker/cgroup/storage 与 GPU 可达性写入
  `<work-root>/state/deployment/deployment-<owner-id>.json`。默认 development 回执永远是
  `production_ready=false`，入口也会打印警告；它是可用的开发/可信主机模式，不是生产验收。

停机时打印 `[run] dual_mode=A 推进 N 轮：[…]；停因=…`。生产只支持每个 Runner turn 推进一个阶段的 A；
未实现 turn 内跨阶段即时提交协议的 B 会在 policy/schema 与 `System` 边界直接拒绝，不会静默按 A 运行。停因见 §6。

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

## 3. 策略与运行契约：policy.yaml

`policies/policy.yaml` 是策略与运行契约的唯一权威（机制代码零硬编码；全量注册表 = 第一部分附录 C）。常见字段：

- `budget`：`B0` 单轮预算、`B_max` 上限、`session_max` 全局成本安全网（ledger.money 求和上限）。
- `flow.tau`：自终止判据①——`score_floor` 分数地板、`consecutive_rounds` 连续几轮低分即停。
- `tree_guard`：`max_decompose_depth` / `max_children_per_node` / `max_open_questions`（问题树规模护栏）。
- `execution`：manifest 命令围栏——`default_timeout_s` / `max_timeout_s`；`path_allowlist` 必须与
  `sandbox.readonly_mounts` 完全相同（外部数据根只读映射）。`sandbox` 还固定 engine unix socket、image digest+ID、
  memory/pids/nofile/日志轮转/单文件/输出/CPU/tmpfs 上限；bundle 锚区给出的 runtime `env_hash` 由这些声明机械派生，
  manifest 只能逐字回引。
- `import_materialization`：GitHub commit 物化的 API/archive/file/tree/submodule 大小与数量上限、
  archive/LFS object redirect host allowlist、LFS object/batch 上限、adapter 路径和 pinned image 中 CPython
  版本/源码 artifact SHA-256。当前 `lfs_policy=fetch`；pointer Git blob、Batch response、下载 OID/size 与最终
  ledger 必须闭合，signed action URL/header 不进入持久复现身份。
- `session.dual_mode`：固定 `A`（一 turn 一阶段）。这是故障恢复/审计边界，不是可调吞吐开关。
- `deployment`：`development` 只记录诚实的非生产回执；`production` 还须给出部署者持有、service account
  不可写的 canonical attestation 路径，并在任何 DB/provider 调用前与 live facts 交叉核。

> **改 policy.yaml 是决策性变更**（影响研究语义/门禁/预算）——生产改动应走评审。改后 `pytest
> tests/test_schemas.py` 会校验它仍合 `schemas/policy.schema.json`。

### 3.1 development / production 部署合同

这套代码不负责创建 VM、service account、Docker daemon 或 GPFS quota；它只在启动时验证这些部署事实。建议先以
默认 development 跑一次，查看 `state/deployment/deployment-*.json` 中的逐项 check，再由运维补齐隔离与 quota。
切到 production 时，把 `deployment.mode` 改为 `production`，并让 `attestation_path` 指向符合
`schemas/deployment_attestation.schema.json` 的 root/部署者只读 canonical JSON。production 最长只接受 300 秒内
签发的 attestation，并把完整内容嵌入 owner receipt 以便重放审计。它必须绑定：

- 非 root service UID/GID、私有 work-root 和 Codex home；
- dedicated VM 的部署证据、实测 hypervisor，以及当前 boot/machine identity；
- service 私有 Codex home/auth、直接 rootless unix Docker socket、daemon ID/root dir、非 fallback cgroup 与资源 limit capability；
- exact work-root/GPFS mount，以及部署 root 从 `gpfs-fileset-v1` 权威 probe 签发的 hard byte+inode quota
  snapshot（进程实时核 mount，不自行调用 GPFS；`df`/`statvfs` 不能代替 quota）；
- `gpu.memory_bytes_by_uuid` 给本 service 分配的 exact UUID 子集（不是整机 inventory）、对应型号/显存/
  compute capability/driver 的 live identity、NVIDIA runtime 与容器 device 可达性，以及 Docker backing store 的实际 headroom。

启动分两段且不新增部署状态机：先只读验证除容器 canary 外的全部静态身份/能力（含 GPU inventory/runtime），
只有 production 的这些检查全部通过后才允许既有
guardian/session recovery；随后用同一 guardian 对 exact UUID 跑一次真实 Docker DeviceRequest canary，严格反核
create 后 inspect 与容器内 `nvidia-smi` inventory，最后才写 final receipt 并开放 SQLite、connector 和 Runner。
v2 receipt 的静态失败形状为 `phase=prerequisite`；完成第二段后为 `phase=final`，其冻结 facts/attestation 位于
嵌套的 `prerequisite` 中，GPU canary 与最终 checks 位于顶层，旧 v1 顶层消费者必须显式迁移。
型号/显存/compute capability/driver 进入可复现 runtime hash，plan 冻结的 CPU/GPU access mode 再派生 workload
env hash；物理 UUID 另进入每次 invocation spec/runtime identity。因此 CPU/GPU 结果不会互相复用，等能力换卡仍可
复用依赖镜像，但任何 GPU invocation 都绑定当次 exact allocation。任一项缺失、过期或漂移都会
fail-closed。development 仍永不产生 production-ready 声明；它可继续 CPU 链路，但 GPU 实验在缺 contract 时会在
创建 session 前明确拒绝。

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
- **轮次恢复点（CP11.4c.3b.1）**：research done cycle 在当轮 τ/global-stop 检查后，import worker、
  failed/aborted cycle 在各自终态边界，用 SQLite online-backup API 发布到
  `<work-root>/state/storage/backups/sha256/`；再从**同一备份**机械渲染 runtime
  `<work-root>/views/{goal,tree,pool,digest}.md` 并提交其独立 Git 仓，最后发布内容寻址 asset manifest 与
  `state/storage/cycles/cN.json` 完成指针。启动会在 connector/Runner 开放前补齐 DB 已终态但 pointer 未发布的
  崩溃缝；同一 cycle 重放不会重复 commit。不可变 `state/storage/genesis.json` 先冻结覆盖起点：
  新系统从 c1 原生覆盖，旧 work-root 首次接入若已有终态历史，只为最新状态建立带
  `bootstrap_before_cycle` 的 adoption baseline，不伪造不存在的历史快照。
  这些 views 是 DB-derived 中文投影，不冒充当前尚未持久化的模型 `cycle_report.md`。manifest 只盘点
  DB 已登记的 checkpoint 与 `execution_log.ref` path+hash，`external_import` 只记 provenance manifest hash；
  本子检查点不声称已盘点 import-materialization CAS/content store。已登记原件不按轮复制、移动或原地压缩。
  backup 与原库同在一个 VEPFS failure domain 时只提供进程/节点以及活库文件误删或损坏
  （storage subtree 仍存）的恢复点，不等同 work-root/fileset 或跨站灾备。

#### 离线快照运维（CP11.4c.3b.2a）

先停止该 work-root 的 run 进程；以下 CLI 会取得同一个 exact instance lease，检测到活跃 owner 会拒绝执行：

```bash
python -m orchestrator.storage_ops --work-root <work-root> verify
python -m orchestrator.storage_ops --work-root <work-root> restore --target <new-work-root>
python -m orchestrator.storage_ops --work-root <work-root> restore --target <new-work-root> --cycle c4
python -m orchestrator.storage_ops --work-root <work-root> gc-plan > /private/outside-work-root/gc-plan.json
python -m orchestrator.storage_ops --work-root <work-root> gc-apply \
  --plan-file /private/outside-work-root/gc-plan.json --expect-sha256 <plan_sha256>
```

`verify` 的机器可读 scope 是 `snapshot_chain_and_retained_sqlite`：它逐轮核 pointer/manifest 父链、views Git
commit/tree 与 applied-plan authority，并对默认受保护的最近 3 代做 hash、SQLite quick/FK/schema 深验；更老但尚未
退役的代只核对象类型/bytes，选中 restore 或进入 GC plan 时才读完整对象。它**不**证明 checkpoint/log/import
objects 的可达闭包。`gc-plan` 自身不改 storage/views，也不在源 work-root 保存计划；CLI 为互斥而产生的 lease
metadata/heartbeat 是唯一控制面写入，shell 重定向也应指向源目录之外。

`restore` 只接受源 work-root 外、尚不存在的目标，并以 no-clobber 方式发布
`research.sqlite + restore.json`。文件系统支持 `renameat2(RENAME_NOREPLACE)` 时是原子目录提升；
当 VEPFS 不支持 rename flags 时，改用目标出现前已耐久的 sibling parent claim + 排他创建目标目录 +
同一 instance lease + `.restore-in-progress` ready marker。后者只在 DB/receipt 已各自耐久落位后，
先解除 parent claim、再删 inner marker；中途退出会
保留不可启动的部分目标供人工检查，不冒充恢复成功；若在目标目录创建前退出，则保留
`.restore-in-progress-<sha256(abs-target)>` sibling claim，操作员检查后再清理。成功的 fallback 目标会多出正常的
`.orchestrator-instance.lock` / `state/orchestrator_heartbeat.json` 控制元数据。receipt 明示
`scope=sqlite_truth_only` 与 `publication_contract=atomic_noreplace_or_lease_fenced_ready`；新 work-root 首次
启动会诚实建立 adoption baseline，不带回原 views Git、snapshot timeline、checkpoint/log/import 原件，
因此不是完整 work-root 或跨站灾备。GC 默认至少保留最近 3 代；
apply 必须同时给 canonical plan 和显式 hash，先持久化不可变 authority，再仅删除 backup CAS 中计划列明的
expired/orphan 文件。即使进程在 authority 与 unlink 之间退出，该代也已逻辑退役且不能恢复，重放只补物理删除；
pointer、manifest、genesis 与 views Git 永不由该 GC 删除。

每次 cycle backup 在创建 temp/pending/Git 前检查 `本次 SQLite bytes/inodes + reserve` 的实时物理 headroom。
production reserve 来自启动时已验证的部署 envelope；`statvfs` 只是此刻的物理余量，不是持续 GPFS hard-quota
监控。

#### 已登记执行日志镜像（CP11.4c.3b.2b.1）

先停止该 work-root 的 run 进程；这是离线检查点操作，不自动挂到每轮终态路径：

```bash
python -m orchestrator.storage_ops --work-root <work-root> mirror-logs
python -m orchestrator.storage_ops --work-root <work-root> verify-log-mirrors
```

`mirror-logs` 只从最新不可变 SQLite snapshot 中枚举身份完整的 `execution_log` 行，在
`state/storage/log-mirrors/objects/sha256/` 发布 level-9、mtime=0、空 filename、OS=255 的确定性
gzip CAS，并在 `indexes/execution-log-<id>.json` 嚻结 DB 行与镜像身份。源文件须仍与登记的
ref/hash/bytes 一致；命令不移动、删除、chmod 或原地压缩它。object/index 均先耐久化再建下游
引用，kill 后重放会补齐 rename→fsync 窗口；验证按登记 raw bytes 上限有界解压，拒绝尾随数据、
多 gzip member、类型/link/mode/hash 漂移。额外 CAS 只会报为 orphan，本命令不删除它。

机器可读 scope 是 `db_registered_execution_logs_only`：它不 glob 未入库失败日志、runner transcript、
guardian capture 或 sandbox session，也不声称覆盖“所有 raw log”。镜像是已登记冷数据的内容副本/
离线校验面，原件仍是冻结 ref 指向的正本，它尚未进入 `restore` 恢复闭包。单次离线扫描随
cycle/log 数和日志总字节线性增长；若每轮全量自动执行，跨轮累计会趋近二次方，因此本设计明确只用于
人工/最终检查点。

import-materialization indexes/objects 及其 dependency-image 传递闭包、registered asset 恢复仍属
CP11.4c.3b.2b.2；完成前不能声称 b.2 或存储治理整体验收。

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

当前是**强隔离已接线、部署/长跑仍待验收的 development 金丝雀态**，足以让真 Codex 端到端跑通并验证研究链路，但还不能
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
  CUDA 用户态库；GPU 真研究须通过 dependency image 锁定所需 CUDA/framework。具备受验 cgroup 的 GPU
  invocation 继续由 memory.max 限 resident host memory，但不设置会误杀 CUDA 大虚拟地址预留的有限
  `RLIMIT_AS`；只有 GPU canary、cgroup 与 memory/CPU/PID limit 三者都通过才推广 GPU sandbox，fallback
  节点仍保留有限 RLIMIT、只运行 CPU workload 且不能成为 production。
- deployment preflight 会把上述差距机械写成 `production_ready=false`。当前节点还以 root 运行、Docker socket
  经 symlink 进入共享 rootless daemon、容器无 NVIDIA runtime、GPFS hard byte+inode quota 无权威 probe，且
  Docker backing store 已接近/达到空间上限；这些都不是代码内的“允许降级”，切 production 会 fail-closed。
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
  子模块还独立执行 allowlist 决策。symlink 仍 fail-closed；Git LFS pointer 只经固定 GitHub Batch endpoint 的
  basic transfer 下载，action URL/header 受 HTTPS host/header 围栏，最终 bytes 重算 SHA-256 OID 与 size。GitHub
  archive 若已展开 LFS object，还会回查 pointer Git blob 后再做同一 OID/size 双核。
  协议口径固定为 Git LFS 官方 [pointer spec](https://github.com/git-lfs/git-lfs/blob/main/docs/spec.md) 与
  [Batch API](https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md)；不从仓库内容推导 endpoint 或凭据。
- 仓库可携 `.meta-research/import-adapter.json` v2/v3：只允许 pinned Python 直接 argv，v2 使用
  `pinned_image_only` 且不安装项目依赖，v3 只接受唯一 canonical `python-wheel-lock.json`，将精确
  wheel bytes 离线安装进可恢复的专用 image。两者都须显式声明 artifact、smoke/eval 和 named factory metrics。
  若 adapter 缺失，系统只把已验 Git ledger 的有界 UTF-8 projection 交给无工具生成会话，再由另一无工具
  会话独立复核；schema、hash echo、dependency contract 和原 adapter compiler 都通过后，sidecar 才内嵌进
  snapshot spec，不写回 Git tree。
  adapter 通过 sandbox smoke + 代码评审后才能以 repository/name 稳定协议家族 ID 注册；同版本
  scope/指标变了会碰撞并拒绝，不能用换 ID 逃避升版。发布快照在
  `<work-root>/state/import-materializations/{objects,indexes}` 内内容寻址，worker DB 只存有界身份。
  旧候选内嵌 `materialization` v1 仍可恢复。
- 物化实现以 `repository_materializer.py` 作为兼容 facade 与单次编排入口；共享 identity primitives、HTTPS
  transport、Git tree、archive/submodule、adapter compiler、content store 分属
  `repository_materialization_common.py` 与 `repository_materializer_{transport,tree,lfs,archive,adapter,store}.py`。
  扩展 LFS、依赖构建或 adapter 生成时应落入对应组件，不得把供应链职责重新堆回 facade。
- 大仓库的 judge prompt 不会整仓读入内存：给出文件数/总 bytes、至多 256 条且合计 32KB 的路径，并按
  adapter/命令入口/Python 优先，在 160KB 总预算（单文件 20KB）内展示内容；其余文件仍受 exact
  manifest hash 闭包约束，但不会冒充已经过语义评审。关键执行路径因截断不可判断时，judge 应 fail-closed。
- 这仍不是“自动复现任意 SOTA repo”：源码投影无法证明唯一 artifact/入口/评估语义、独立复核拒绝或
  sandbox smoke 失败都不会入池；除 canonical Python wheel lock 外的 `requirements.txt`/Poetry/Conda/Node/Rust
  lock 只作证据展示，不会被生成器安装或冒充可复现环境。部署级 quota/cgroup/device 合同仍属后续检查点。
  LFS 支持也只覆盖 GitHub 固定 Batch endpoint、
  policy allowlist 内 basic HTTPS transfer，不接受仓库自定义 `.lfsconfig` endpoint/transfer adapter。
- CP11.3c 的 120 轮是无真实 provider 工作负载的控制面/投影回归；尚未完成跨节点 VEPFS 双 owner 实机竞态，
  也尚未运行数百轮（最终验收下限 ≥200）含真实 Codex/import/训练与 owner-kill/daemon-loss/预算失败注入的 soak。这两项完成前，
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
