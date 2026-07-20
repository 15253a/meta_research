# meta-research 元循环系统 —— 本机 Web 产品与运维手册

自主研究编排器：一条命令让真 Codex 全自动跑「出题 → 分解 → 建基线/变体 → 训练 → 评估 → 双评审 →
关问」的研究元循环。**确定性编排器从不推理**：Idea/Plan/Bundle/Reasoning 由各自的常驻主
Codex 在一个连续 stage turn 内思考、获取 MCP 即时反馈并修订；模型不直接写数据库，核心状态只由
编排器消费精确阶段回执后在短事务中提交。SQLite 是唯一真相（36 表冻结 DDL + 三重锁）；普通受信调用由外部 guardian 管理子孙树，
bundle/import 的不可信代码只进入 exact-pinned Docker sandbox；事务边界可从 DB 无半写恢复，执行边界以耐久
receipt 证明本机子树与 daemon container 均排空（适用条件与残余边界见 §7）。

> 设计真相唯一在 `../reference/第一部分-系统架构设计.md`；本 README 是**操作面**（怎么跑、怎么配、怎么
> 观测/干预、边界在哪）。构建历史见仓库根 `ROADMAP.md` / `build_log/`。
> T1/T2 sealed-holdout/one-shot 的部署方内部边界见 [qualification runbook](QUALIFICATION.md)；它不是
> 部署后要求研究用户手写数据合同或操作后端路径的使用步骤。T1 已机械闭合
> A→B→一次性 C（exact sandbox promotion）→root audit/immutable verdict ledger→D 准入链，但科学审核与
> 真实生产环境的人类终验仍须独立完成，不能由机器回执代签。
> 目标环境的完整串联、原始证据命名及 `fixed_and_test` 交接见
> [production acceptance runbook](PRODUCTION_ACCEPTANCE.md)；该 runbook 不是通过证明。

Idea 发散已换为替换时 WildIdea `main` 的最新上游提交
`6ff66ada15b0047b2e03d229f2e9543c542df598`，并按该 commit 固定；安装和运行不会临时 `git pull`。
上游 Skill 正文自标的 “v1.3” 只是仓库内文本版本，不作为本系统的版本身份。系统只复用其
source-first/9 槽/repair→reangle/批次多样性机制。正常 Idea 只有一个顶层主 turn：它调用 Idea-only
`wildidea_expand` 取 pinned 槽位/阈值，按需调用 `wildidea_search` 取冻结检索回执，然后让一个干净子
reviewer 评分并直接提交 `idea_set.json`。这两个工具都不启动额外模型；普通路径不再使用两个顶层
generate/audit 会话或外部 merge。它不生成 WildIdea HTML，也不运行上游联网 helper。
普通 development/production 阶段会加载本机
Codex 配置，不批量禁用 shell/apps/plugins/MCP/browser/computer/image/multi-agent，并同时开放 live Web 与
命令联网；其可写区是一次性 workspace，quest/SQLite/gate/receipt 仍只读，耐久修改继续走信封与门禁。
adapter/交互应答和 qualification 的显式隔离会话仍只保留 live Web，关闭宿主工具。
WildIdea 的白名单 provider 会把原始响应与规范化结果写成 quest 内内容寻址快照，并将同一冻结证据回给
Idea 主 turn 的干净 reviewer、最终 artifact 和 SQLite。
Codex 的 live Web 仍只作生成侧易失辅助，不能自行提供 P6 hash；受控检索只标记
“联网粗查已启用·文献级待人工验证”，零命中或模型判断都不会冒充“查重通过”。

## 0. 一分钟跑起来

```bash
cd meta-research
python -m pip install .
docker pull docker.io/library/python@sha256:f5cf0344c9886ff24d34797578d5d7dd6e8911ae0fe5962bb55d0f89603ec361
meta-research
```

安装完成后，无论当前目录在哪里都可运行 `meta-research`；源码环境仍可用
`python -m orchestrator.web_app`。服务会自动打开已认证的 `127.0.0.1` 页面；若桌面环境未能自动打开，启动终端会
直接显示可复制到本机浏览器的授权链接，不要求用户查找任何后端 capability 文件。该链接等同本机会话凭证，
不要转发、公开或粘贴到工单/聊天记录。此后用户只在 Web 中完成：新建/切换研究任务、填写目标或选模板、
提供初始文件、选择本机已有数据集/参考资料目录、查看预检、启动/停止任务、讲解员问答、pause/resume，以及
运行期文件补交。用户不需要回到后端创建 quest、填写 `source_ref`、编辑 JSON 数据合同或查看日志文件。

初始资料有两种并存入口：小文件可在浏览器选择文件/文件夹，由系统分块校验并托管到只读 corpus；本机已有的
大数据可在向导填写绝对目录（数据集与参考资料分开），服务以当前本机 UID 做 no-follow 只读枚举，在发布边界
逐文件 SHA-256，并为普通任务自动派生 host argv 围栏与 Docker 只读挂载。API 只回显目录名称/数量/容量，
内部绝对路径和清单不会作为浏览器能力返回。目录在发布后发生变化时，下一次启动会在 Web 明确拒绝并显示原因。

默认数据落在当前 Meta-Research 安装目录的 `runtime/`：每个任务独占
`runtime/quests/<quest-id>/`，跨任务的 Web/创建状态位于 `runtime/state/`。这样代码、任务数据库、cycle、实验产物
和执行记录跟随同一套本机部署，不会隐式散落到用户主目录。原始大数据仍只按用户选择的原路径只读引用，不复制到
`runtime/`。只读安装或确需搬迁时可显式使用 `--data-root`（或 `META_RESEARCH_HOME`）改位置。离线本机模式默认
不投递外部通知；需要 connector 时传 `--connector-profile`。`t1-eeg-universal` 只有在 Web 的部署检查显示“安全评测已就绪”时可选；
合同、research/evaluator UID、sealed truth 和一次性边界由部署组件内部生成/选择，不能从浏览器提交。科学审核和
真实目标环境终验仍需独立完成，机器预检不会冒充资格通过。

普通真实 EEG 研究不需要 qualification 合同：选择 Web 中的“本机多数据集 EEG 研究（LODO）”，填写 SEED、
DREAMER 等数据目录和参考资料目录即可。该模式可真实训练、评估和做 LODO，但会诚实地把 DREAMER 视为普通
研究数据；只有要声称“整个研究过程从未见过 DREAMER 标签、且只评一次”时，才需要可选的 sealed 评测服务。
这一区分不会把运维步骤转嫁给研究用户。

当前本机验收部署的资源合同是单卡 80 GiB：`gpus=1`、`gpu_mem_gb=80`、
`gpu_target_policy=required`，development 选卡面收窄为 `allowed_device_indices=[0..6]`（GPU 7
不授权）。因此新 plan 的每个 target 必须显式 `gpu_required=true`；编排器不会从
`gpus` 数字暗推或改写 plan，而是拒绝与显式三态合同冲突的产物。物理 index/UUID 由部署
从允许集中选取并经 exact DeviceRequest canary 核验，模型不指定卡。`disk_quota_gb=1`
仍是 quest 工作区/浏览器托管输入的保守基线；Web 选择的本机数据目录原地只读使用，不复制进该额度。

开发者自验可另装 `requirements-dev.txt` 后运行 `python -m pytest tests/ -q`。底层
`orchestrator.run` / `quest_run` CLI 保留给运维、恢复与隔离测试，不是部署后的普通用户流程。其参数语义如下：

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
supervisor（整树排空 + terminal receipt）、Bundle 非 daemon 的 gate/publication 收尾线程、只读/写 DB 连接、
heartbeat，最后才释放 flock。close 返回错误必须按
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

真执行走本机 `codex exec`。每个正常 stage 只启动一个连续进程/turn；runtime MCP 在该 turn 内做只读预检、
阶段提交和 Bundle 执行反馈。耐久 provider id 只用于宿主进程灾难恢复；超时且未观测到 id 时 fail closed，
不会新建会话重试。工程配置走**环境变量**（模型/二进制是工程事实，不进
`policy.yaml` 旋钮注册表）：

常驻阶段与 runtime MCP 的关键边界是：

- Idea 的 WildIdea 扩展/检索是 Idea-only 数据工具，不启动额外模型；Idea/Plan/Bundle 各在主 turn 内
  启动恰好一个干净 reviewer，强度为 1。Bundle 的 code/result 审查复用同一子会话。
- review 强度非零时，`submit_stage_artifact` 会机械要求当前 live stage/target/purpose 已有对应
  `record_review` 回执；缺失错误直接回到同一个主 turn。该门只核评审确已记录，不启动另一个模型、也不替
  主智能体解释或改写 reviewer 意见。
- Plan 在最终提交前调用只读 `preflight_plan`；它不写 DB，也不预占 baseline。旧
  `register_baseline` 能力已移除；真正 claim 只在编排器消费已提交 Plan 回执的核心 gate 事务中发生。
- Bundle 提交完整包后把 exact submission receipt 交给 `bundle_execute`，用 `bundle_status` 读权威进度，并在
  当前 turn 内修包重跑；只有冻结计划本身不可执行才调用 `bundle_replan`。旧 event/operator action 不在正常路径。
- Reasoning 汇总所有成功、失败、dependency wait 与 replan 分支；只有消费其最终回执的 Reasoning 核心事务
  能终态化 cycle/question 与 selection。
- MCP 提交在最终短事务中重查 cycle/stage/target/plan 绑定；已 commit 的 submission receipt 不会被随后 TTL
  反向过期，而 revoke/过期先线性化时会拒绝提交。历史空 provider receipt 不会覆盖后续唯一有效 id；
  若从未观测到 id，恢复必须 fail closed。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `METARESEARCH_CODEX_BIN` | `codex-chatgpt` | 本机已认证的 codex 包装 |
| `METARESEARCH_CODEX_MODEL` | `gpt-5.6-sol` | 当前 ChatGPT Codex 可用模型名 |
| `METARESEARCH_CODEX_EFFORT` | `max` | 推理力度 |
| `METARESEARCH_RUNNER_TIMEOUT_S` | `3600` | 普通 Codex 调用超时。跨整个 cycle 的 Bundle 常驻主 turn 绑定 owner 生命周期，不受该墙钟截止；官方 smoke/train/eval 仍各自使用 manifest watchdog。 |
| `METARESEARCH_QUERY_CODEX_BIN` | `/usr/local/bin/codex` | query/adapter generation/review 的 tool-free CLI；工人 PATH 固定为 `/usr/local/bin:/usr/bin:/bin`，兼容该入口的 `env node` shebang |
| `METARESEARCH_QUERY_RUN_AS_USER` | root 运行时默认 `codexro`；non-root 默认当前账户 | 可选 UID 隔离；production non-root service 无特权切换 UID，不设置或填当前 service account |
| `METARESEARCH_QUERY_CODEX_HOME` | 专用账户的 `~/.codex` | 仅 root 显式/默认降权到独立 UID 时使用；同 UID production 使用已验证的 `CODEX_HOME` |
| `METARESEARCH_GITHUB_TOKEN` | — | 可选的 GitHub 只读 API token；仅 plan 明确触发 repo discovery/direct resolve 时读取，不落 policy/DB/回执 |
| `HTTP_PROXY`/`HTTPS_PROXY` | — | 真 Codex 需要（本机 7890）；Runner 仅从显式白名单转发代理/TLS 环境，不整包继承宿主环境 |

冒烟自检（一条命令跑通至少一个完整 attack 轮）：
```bash
python -m orchestrator.run --system-root . --work-root /tmp/smoke --max-cycles 6
```

## 3. 策略与运行契约：policy.yaml

`policies/policy.yaml` 是策略与运行契约的唯一权威（机制代码零硬编码；全量注册表 = 第一部分附录 C）。常见字段：

- `budget`：`B0` 单轮预算、`B_max` 上限、`session_max` 全局成本安全网（ledger.money 求和上限）。
- `flow.tau`：自终止判据①——`score_floor` 分数地板、`consecutive_rounds` 连续几轮低分即停。
- `flow.retry.bundle_repair`：同一冻结 plan 内每个 target 的实施自愈上限；当前默认为 `null`，
  表示同一 Bundle 主 turn 内的工程修复不按轮次截断。只有主 Codex 依权威日志判定冻结 plan 本身不可执行时
  才请求 replan 并交必经 Reasoning；用户停止、target 预算或 full watchdog 仍是独立安全界。
- `tree_guard`：`max_decompose_depth` / `max_children_per_node` / `max_open_questions`（问题树规模护栏）。
- `execution`：manifest 命令围栏——`default_timeout_s` / `max_timeout_s`；`path_allowlist` 必须与
  `sandbox.readonly_mounts` 完全相同（外部数据根只读映射）。`sandbox` 还固定 engine unix socket、image digest+ID、
  memory/pids/nofile/日志轮转/单文件/输出/CPU/tmpfs 上限；bundle 锚区给出的 runtime `env_hash` 由这些声明机械派生，
  manifest 只能逐字回引。
- `resources`：`gpus` / `gpu_mem_gb` 是部署 allocation 请求；`gpu_target_policy`
  用 `planner_select|required|forbidden` 独立冻结 plan 中的 GPU mode，
  `allowed_device_indices` 只窄化部署选卡面。库存数量不会被当成 target 授权。
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
  **系统停在该轮游标处等待**（全局等待，无自动超时，但 owner 仍消费控制动作）。用户在 Web 请求卡上选择
  文件/文件夹并点“上传并提交”；页面做 4 MiB 顺序分块与增量 SHA-256，服务端以持久幂等发布回执把资料接纳到
  quest 私有上传区，再由 daemon 原子迁入 `<work-root>/input/user_provided/<request_id>/`。浏览器不提交
  `source_ref`，用户也不应人工写 `work/uploads` 或托管输出。无法提供时在 Web 取消并留下理由。
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
启动会诚实建立 adoption baseline；`registered_path_roots` 只为 append-only DB 中的旧绝对 ref 保存受审路径
lineage，不改写 checkpoint 行。target 不得等于、位于或包含当前/任一历史 lineage root；该检查在创建 target 或
parent claim 前完成，避免发布一个随后无法解析自身旧 ref 的二次恢复根。基础 `restore` 不带回原 views Git、snapshot timeline、checkpoint/log/import 原件，
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
gzip CAS，并在 `indexes/execution-log-<id>.json` 冻结 DB 行与镜像身份。源文件须仍与登记的
ref/hash/bytes 一致；命令不移动、删除、chmod 或原地压缩它。object/index 均先耐久化再建下游
引用，kill 后重放会补齐 rename→fsync 窗口；验证按登记 raw bytes 上限有界解压，拒绝尾随数据、
多 gzip member、类型/link/mode/hash 漂移。额外 CAS 只会报为 orphan，本命令不删除它。

机器可读 scope 是 `db_registered_execution_logs_only`：它不 glob 未入库失败日志、runner transcript、
guardian capture 或 sandbox session，也不声称覆盖“所有 raw log”。镜像是已登记冷数据的内容副本/
离线校验面，原件仍是冻结 ref 指向的正本；只有下文 `restore-with-registered-assets` 会从该镜像 hydration
日志原件，基础 `restore` / import-only 恢复仍不做。单次离线扫描随
cycle/log 数和日志总字节线性增长；若每轮全量自动执行，跨轮累计会趋近二次方，因此本设计明确只用于
人工/最终检查点。

#### Import materialization 离线闭包与恢复（CP11.4c.3b.2b.2）

先停止源 work-root；核验命令从所选 retained SQLite snapshot 出发，不读取活库：

```bash
python -m orchestrator.storage_ops --work-root <source-work-root> \
  verify-import-materializations
python -m orchestrator.storage_ops --work-root <source-work-root> \
  verify-import-materializations --cycle c4

# 推荐的一步恢复；target 必须不存在：
python -m orchestrator.storage_ops --work-root <source-work-root> \
  restore-with-import-materializations --target <new-work-root> --cycle c4

# 仅用于续完已经存在、且 restore.json 绑定同一 source/cycle 的 SQLite target：
python -m orchestrator.storage_ops --work-root <source-work-root> \
  restore-import-materializations --target <existing-restored-root> --cycle c4
```

`verify-import-materializations` 的 scope 是
`sqlite_registered_repository_and_dependency_cas`。它沿 immutable DB 中的
`build_target → import_worker_cycle → selected_for_materialization → external_candidate` 精确重建每个
candidate index，再把 `plan_ref.repository_snapshot_hash` 闭合到 repository object；v3 继续闭合到 exact
dependency-image receipt/archive。repository、index 与 dependency inspector 只读取冻结文件和 provider
协议硬上限，不调用 Docker、网络或当前运行策略。legacy embedded target 与尚无 plan_ref 的 target 会分别计数，
不能伪装成 repository root；新式 plan_ref 缺字段、内外 hash 不一致或 exact index 缺失会 fail-closed。
无 DB 根的 object/index 只报告为 orphan，不删除。

`restore-with-import-materializations` 在 SQLite target 第一次可见前就放入 root
`.restore-in-progress` continuation marker，并在同一源 lease 内续完 import CAS；不存在“两条命令之间可启动半恢复
target”的窗口。VEPFS fallback claim 绑定 exact source snapshot/cycle/resolved target，claim、marker、SQLite 或
receipt 任一早期窗口退出后均按既有 exact bytes 续接；已有 marker 的现场由 exact marker + target flock 特权接管，
不另建恢复状态机。
`restore-import-materializations` 只接受同一 source snapshot 的现有 target，用于续完/重放，不是新灾备恢复的推荐入口。
发布顺序为 dependency object → repository object → exact canonical index → completion receipt；复用条目也重新
fsync。进程在任一窗口退出时，普通启动仍会拒绝 target，同一命令可重放。容量门按 target filesystem block size
为文件内容、每个目录和每个目录项保守预算，再加临时 inode/余量。完成 receipt 的 scope 明示
`repository_and_dependency_cas_only`：本命令不运行 image、
不删除源/孤儿，也不恢复 execution-log 正本、checkpoint、content store、views Git 或完整 work-root；日志冷副本仍由
上节 mirror 命令独立核验。因此本项闭合的是 import CAS 灾备，不外推为 CP11.4c.3 的两节点/≥200 轮生产验收。

#### Registered checkpoint/log 原件恢复闭包（CP11.4c.3b.2b.3）

最终检查点先停源 owner，一次性冻结并核验最新 high-water 的全部 DB-registered checkpoint 与 execution log：

```bash
python -m orchestrator.storage_ops --work-root <source-work-root> \
  mirror-registered-assets
python -m orchestrator.storage_ops --work-root <source-work-root> \
  verify-registered-assets

# 推荐完整组合：SQLite + checkpoint/log 原件 + import repository/dependency CAS
python -m orchestrator.storage_ops --work-root <source-work-root> \
  restore-with-registered-assets --target <new-work-root>

# 只用于续完同一 source/latest high-water 已经留下的 fenced target：
python -m orchestrator.storage_ops --work-root <source-work-root> \
  restore-registered-assets --target <existing-restored-root>
```

checkpoint 镜像从最新已深验 SQLite backup 的 `checkpoint` 行枚举，只接受 source path 可映射到当前
work-root/path-lineage 的单链接常规文件；raw bytes 以登记 SHA256 直接进入
`state/storage/checkpoint-mirrors/objects/sha256/`，per-row immutable index 另绑 variant/key/ref、artifact type、
origin/provenance 与相对 hydration path。同内容 checkpoint 只存一个 CAS object，命令不改原件；它与日志镜像一样
是离线/最终检查点操作，不挂每轮终态，避免大 checkpoint 的跨轮重复复制。`state/`、`views/`、SQLite/restore/lease
控制文件名是运行时可写或派生的保留 namespace，不能登记成 hydration 目标；合法 checkpoint/log 应位于研究产物/
staging namespace，避免恢复后被 heartbeat、console、snapshot publisher 等控制面改写。

`restore-with-registered-assets` 只接受最新 mirrored high-water；历史 selector 会在创建 target/parent claim 前拒绝。
SQLite target 第一次可见前放入独立 `registered_asset_restore_required` marker，随后按已冻结相对路径以 no-clobber
方式 hydration checkpoint 与日志 raw 原件，再续 import CAS。checkpoint/log 同路径但不同 hash/bytes 会在写 target
前拒绝；已存在文件只能 exact hash/bytes/mode 复用，绝不覆盖。每个文件和 completion receipt 都先 fsync；kill 后
可在同 marker + target lease 下重放。独立 `restore-registered-assets` 也只接受该 registered continuation receipt/marker；
import-only target 会 fail-closed，不能事后用一个较窄 marker 绕过最终 registered completion 复验。

最后的 import 步骤不能凭自报空 receipt 解除 marker：它会从源 snapshot/mirror **重新构造 exact files authority**，
与 target immutable completion receipt 逐字段对比，并逐个重读 hydrated 文件的 owner/type/link/mode/hash/bytes；
任一缺失、篡改或尚未 hydration 都保留 `.restore-in-progress`，普通启动继续 fail-closed。旧
`restore-with-import-materializations` 仍是明确的 import-only 较窄合同，不能冒充本组合恢复。

该闭包覆盖 SQLite 登记的 checkpoint、execution-log 原件以及 DB 可达 repository/dependency import CAS；旧绝对 ref 通过
`restore.json.registered_path_roots` 映射到当前根，二次恢复仍保留完整 lineage，不修改 append-only schema。
它仍**不** glob runner/guardian/sandbox/qualification authority、未登记失败产物、用户 uploads/input、views Git 或
connector cursor，也不把 source 内同一 VEPFS storage subtree 冒充完整 work-root/fileset/跨站 DR。整根或站点丢失
仍须目标环境提供独立故障域归档位置并另做演练。

#### Canonical evidence pack 与单轮续跑探针（CP11.4c.3c.3）

证据包是现有运维原语之上的**只读汇总层**，不是第二套 restore/launcher。下面的窄口径示例仍走
`restore-with-import-materializations`，研究仍只走既有 `orchestrator.run`；已经成功完成
`restore-with-registered-assets` 的 target 也可作为同一 SQLite/import-CAS 单轮 resume probe，但 v1 不因此代证
registered hydration。推荐对最新 high-water 做下面的
完整顺序；旧 cycle 不能和“最新日志镜像”形成同一个精确闭包，因此 v1 不提供历史 cycle 选择器：

```bash
# 1. 停止 source owner；先创建并验证冷日志镜像。
python -m orchestrator.storage_ops --work-root "$SOURCE" mirror-logs

# 2. target 的父目录须存在，target 自身须不存在。
python -m orchestrator.storage_ops --work-root "$SOURCE" \
  restore-with-import-materializations --target "$TARGET"

# 3. SQLite-only restore 不含 connector producer/cursor authority；必须禁用出站，避免历史通知重投。
python -m orchestrator.run --system-root "$SYSTEM_ROOT" --work-root "$TARGET" \
  --max-cycles 1 --once --no-outbound

# 4. source/target owner 都停止后打包；output-parent 必须在二者之外。
python -m orchestrator.evidence_pack pack \
  --source-work-root "$SOURCE" --resume-work-root "$TARGET" \
  --output-parent "$EVIDENCE_DIR" \
  --canary-root "$CANARY_ROOT" --canary-run-id "$CANARY_RUN_ID" \
  --canary-scope two-node-process-crash

# 5. 可在 source/target 都不可用的环境中只读复验。
python -m orchestrator.evidence_pack verify \
  --pack "$EVIDENCE_DIR/<manifest-sha256>.evidence"
```

`$TARGET` 与 `$SOURCE` 是不同绝对路径。若步骤 3 使用 production policy，部署者必须为 `$TARGET` 另签一份
仍在有效期内、精确绑定 target work-root 的 attestation；源目录 attestation 不能复用。若只验证 v1 的
SQLite/CAS 单轮恢复能力，可改用其余执行配置相同但 `deployment.mode=development` 的
`$SYSTEM_ROOT`，并保留 `production_ready=false`：该 probe 本来也不证明 production connector 或完整生产恢复。

包是未压缩的 owner-only 内容寻址目录：`manifest.json + READY.json + objects/sha256/<digest>`。manifest
严格列举每个 object，目录名等于 canonical manifest SHA256；verifier 拒绝缺失/多余对象、未知根条目、
symlink/hardlink、权限/owner/bytes/hash 漂移，并以流式 hash 和显式文件/总字节硬上限处理大对象，不把整份
SQLite/image archive 读入内存。SQLite backup 仍走既有 quick/FK/schema/terminal 深验；日志镜像在包内直接做
有界单-member gzip→登记 raw hash/bytes 复验，不会回开 receipt 中的 source 绝对路径。manifest 冻结经 source
`restore.json` 校验得到的 `source_registered_path_roots`；离线 verifier 只用它把合法旧绝对 log ref 映射为镜像
index 的当前相对路径，并拒绝非 canonical、重复、嵌套或歧义 roots。reachable repository
CAS 和 dependency CAS 中由 receipt 绑定的恢复语义文件闭包与原 verifier report 一并冻结；其 provider
语义是 pack 时已由现有 inspector 验证，离线包不联网、不调 Docker。builder 的 install/build/save
诊断日志、process pointer 和动态 sandbox metadata 没有被 dependency receipt 内容绑定，因此不冒充恢复闭包、
不进入 evidence pack。离线 verifier 会把 asset inventory、repository root/target、repository ledger 投影/
count/bytes 与包内 SQLite 精确对账，并由 DB-bound execution-image capability 加 dependency receipt/
installed manifest 重建其精确语义文件集合；这验证冻结 bytes 的闭包，但不重复 provider 的联网/
运行时语义检查。目录 hash 是内容身份而非来源签名，生产
留证时须把 `<manifest-sha256>` 另存到变更单/不可变审计系统。

`one_cycle_resume_probe_verified=true` 不是根据 exit code 推断。packer 要求 `restore.json` 精确绑定 source 最新
cycle/manifest/backup/path-lineage，offline verifier 再要求 receipt roots 与 manifest
`source_registered_path_roots` exact equality。target 无 marker/parent claim，target snapshot chain 从 source cycle 建立
`adoption_baseline`，并且**恰好**新增一个 `status=done AND route IS NOT NULL` 的研究 cycle 及其深验 snapshot。
该轮还须至少有一条 `status=success` 的研究阶段 `runner_call` 及同 cycle 的关联 ledger；restore 后 0 轮、
failed/aborted、pause/file-request/global-stop 都不会通过。此结论与 fault final 分开；packer
永不把 fault receipt 原有的 `signal_exactly_once=false` / `recovery_verified=false` 改成 true。

v1 始终明示 `real_codex_resume_verified=false`、`qualification_receipts_verified=false` 和
`full_restore_verified=false`。仓库回归用注入式确定性 worker 证明的是原状态机/恢复组合，不冒充真实 Codex、
connector 交付或生产部署。若 source 带 qualification contract，普通 resume probe 会 fail closed：现有 restore
不会带回 firewall，而 final-consumed contract 即使安全带回也应禁止继续研究。无 resume 时可把 qualification
内部 receipt 作为 `receipt_only` 冻结，但不声称 sealed truth/source/runtime/GPU 闭包完成。

evidence-pack v1 本身仍不打包 checkpoint mirror、registered hydration completion、用户上传资产或完整
restore/path-lineage receipt 证明；`source_registered_path_roots` 只闭合包内 log-mirror identity，不能升级恢复
scope。因此这些仍按该包的窄口径列入 `unresolved_registered_assets`；即使另行成功执行了上节组合恢复，
也必须把其原始 CLI/receipt 作为 building 终验证据单独保存，不能让 v1 pack 越权代证。故一次成功续跑只证明所选
下一轮在 SQLite/import-CAS 切面可推进，不等于完整 work-root/跨站 DR。source 下已完成的 fixed-linear fault schedule 会自动进入包；
生产 two-node shared-fs canary 必须像上方命令同时指定
`--canary-root/--canary-run-id/--canary-scope two-node-process-crash`。只给前两个参数会因默认
`single-node-prerequisite` scope 与冻结的
two-node contract 冲突。local receipt 仍保持
`two_node_verified=false`，任何 canary 都保持 `infrastructure_fence_verified=false`。

### 4.1 本机 Web 产品（任务设置 + 运行 + 观测与交互）

`web_app` 是部署后的主入口。它管理 quest 草稿/输入发布和本机 owner 进程，但对已运行研究库仍以 `mode=ro`
读取；人工动作只写入站 spool，研究真相始终只有对应 quest 的 `run` owner 一个写者。Web server 只会停止它在
本次进程内亲自创建并仍持有能力的进程组；重启后观察到的外部 owner 不会仅凭 PID 被误杀。

```bash
python -m orchestrator.web_app
# 默认任务根：<当前 Meta-Research 安装目录>/runtime
# 可选搬迁：--data-root /path/to/product-data --port 8765
```

页面顶栏可新建、切换、启动和停止 quest；每个 quest 都拥有独立目录、SQLite、baseline 池、问题树和 inbox。
发布操作先验证 corpus/本机目录并写 ready receipt，之后才允许 owner 启动；同一 quest 的第二 owner 仍会被
instance lease 拒绝。运行失败的限长脱敏诊断可在顶栏直接查看，不需要打开 `state/web-owner.log`。

入口会自动用 capability fragment 打开默认浏览器。在默认的多任务本机产品模式下，用户直接打开裸的 loopback
地址时也会由服务执行一次本地 fragment 引导，不需要回终端查找 token；这是第一版单用户、本机安装假设下的
产品入口。页面读取后立即清掉 fragment 和引导标记，并只把 capability 留在本标签页的 `sessionStorage`；它不会
进入 query、cookie 或 `localStorage`，服务也不会提示普通用户读取内部 token 文件。单 quest 管理服务仍要求显式
授权链接，不启用裸地址引导。
全部 `/api/*` 请求都要求 `Authorization: Bearer <token>`。远程使用仍只允许 SSH
tunnel（例如 `ssh -L 8765:127.0.0.1:8765 <host>`），不得把服务直接绑定到局域网地址。
正常模式下，缺 capability、尚未成功取得第一份 `/api/db` 真快照、网络中断、401 或 5xx 都会持续显示
全屏连接保护层并禁用所有 live 控件；只有后续 `/api/db` 成功才解锁。内嵌演示数据只在显式打开
`http://127.0.0.1:8765/?demo=1` 时可操作；demo 模式不发送任何控制 API，不是断线降级模式。

所有 POST（`/api/message`、`/api/query`、`/api/directive`、`/api/file-request`，以及多任务的
`/api/quests`）还必须带恰一个
`Idempotency-Key: <32 位小写十六进制>`。浏览器会在 fetch 前先将键写入 `sessionStorage`；网络结果不明或
5xx 时保留并为同一 operation 重用，只有 2xx 回显完全匹配的幂等键或确定性 4xx 才清除。
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
- **显式控件**：待确认硬指令可直接点确认/拒绝；pending 文件请求直接从页面选择并上传，或点 cancel。
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
  产物 → 有界修复后目标 `engineering_blocked`/`failed` + 记账，绝不死循环）。

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
  attestation，不能靠 0600/HMAC 自我证明。普通受信 Runner 也只传 PATH、locale、`CODEX_HOME`/HOME/TMPDIR
  与代理/TLS 的显式环境白名单，不向 shell-capable 模型进程转发 connector/GitHub/cloud secrets；tool-free
  query/repository-adapter 与 qualification 隔离工人还始终使用空临时 cwd、关闭全部 host/web/plugin 工具并严核
  trace；production
  的 non-root 工人与 service 同 UID，由同 UID guardian
  排空整树。root 开发环境默认另用 `codexro`，跨 UID 时 guardian 必须以 root 运行才能完整终止。
- fork child 会丢弃非目标 lease FD；owner 死亡同时由 pipe EOF 与 `PR_SET_PDEATHSIG` 触发 guardian。SQLite
  journal mode 按 live mount 机械选择：已知本地文件系统用 WAL，GPFS/未知共享文件系统用 `DELETE` rollback
  journal + `synchronous=FULL`。这是因为 [SQLite WAL 官方契约](https://www.sqlite.org/wal.html) 要求全部进程
  位于同一 host，不能用一次共享盘 canary 把跨 host WAL 变成受支持合同。当前 work-root 位于共享 VEPFS，
  同机 owner-kill/fence 可做部署 canary；跨节点只允许 crash-stop 后串行接管，仍须在目标 VEPFS/挂载参数上做
  “A 持锁时 B 必拒、A 被基础设施真正 fence 后 B 才接管”的验收。网络分区但旧主仍存活不由 heartbeat/flock
  自证安全，必须由 VM/基础设施 STONITH；单机测试不能代替它。
- 薄 canary 已提供一个固定、前台、无 daemon 的操作入口。先在新的绝对空目录跑同机先决检查（结果固定
  `two_node_verified=false`）：

  ```bash
  python -m orchestrator.shared_fs_canary local \
    --canary-root /absolute/new-canary-root \
    --run-id 0123456789abcdef0123456789abcdef
  ```

  真两节点检查需在两个节点上并发执行下面两个 `node` 命令；二者必须使用目标 GPFS 上**相同绝对目录**、
  相同 run ID 和 timing 参数。holder 可创建该空目录，contender 会等待 immutable contract：

  ```bash
  # node A
  python -m orchestrator.shared_fs_canary node --role holder \
    --canary-root /gpfs/meta-research-canary/RUN_ID \
    --run-id 0123456789abcdef0123456789abcdef

  # node B（与 node A 并发）
  python -m orchestrator.shared_fs_canary node --role contender \
    --canary-root /gpfs/meta-research-canary/RUN_ID \
    --run-id 0123456789abcdef0123456789abcdef

  python -m orchestrator.shared_fs_canary verify \
    --canary-root /gpfs/meta-research-canary/RUN_ID \
    --run-id 0123456789abcdef0123456789abcdef
  ```

  verifier 只在 machine/boot 均不同、挂载为 GPFS、SQLite 为 `DELETE` 且 lease/guardian/rollback/FD
  全部 receipt 闭合时写 `shared_fs_ready=true`。该结果只验证 owner 进程 `SIGKILL`，始终写
  `infrastructure_fence_verified=false`，不能替代 STONITH 或网络分区验收。local contract 与 two-node
  contract 不可互相升级。two-node 默认给 guardian fence observation 5 秒窗口；若目标 GPFS 的 durable
  receipt 可见性更慢，两个角色须显式传入相同且小于总 timeout 一半的更大 `--guardian-grace-s`，超窗会
  fail closed，不会把未观测到的 fence 写成通过。
- 故障 soak 使用另一个同机前台 sidecar；它不启动、不重启也不调度 `orchestrator.run`。v1 只在全历史唯一的
  running execution receipt 上执行预声明的 `kill_owner` 或 `kill_execution_payload`，不接受 shell、任意 signal、
  DAG、随机动作或远程 kill。Terminal A 仍由操作者或 systemd 启动现有 resident owner；下面是**离线演练**
  命令，真实 soak 应去掉 `--no-outbound` 并沿用部署原本的 connector 参数。selector 已知时先不要执行它：

  ```bash
  set -eu
  export SYSTEM_ROOT=/absolute/meta-research
  export WORK_ROOT=/absolute/research-work
  export SOAK_BASELINE_FILE=/private/outside-work-root/soak-baseline.txt
  DB="$WORK_ROOT/research.sqlite"
  if [ -e "$DB" ]; then
    if ! SOAK_BEFORE="$(sqlite3 -readonly "$DB" \
      "SELECT count(*) FROM cycle WHERE status='done' AND route IS NOT NULL;")"; then
      echo "soak baseline query failed" >&2
      exit 2
    fi
  else
    SOAK_BEFORE=0
  fi
  case "$SOAK_BEFORE" in ''|*[!0-9]*) echo "invalid soak baseline" >&2; exit 2;; esac
  (umask 077; set -o noclobber; printf '%s\n' "$SOAK_BEFORE" > "$SOAK_BASELINE_FILE")
  ```

  Terminal A/systemd 再启动 resident owner；研究达到 max-cycles 后它会有意继续提供 query。fault sidecar 可以
  杀死它，由 service manager 以同一 work-root 重启：

  ```bash
  export SYSTEM_ROOT=/absolute/meta-research
  export WORK_ROOT=/absolute/research-work
  python -m orchestrator.run \
    --system-root "$SYSTEM_ROOT" --work-root "$WORK_ROOT" \
    --max-cycles 200 --no-outbound
  ```

  全部预声明故障/重启完成且 resident owner 已干净停止后，operator 在独立 shell 做最终计数门：

  ```bash
  set -eu
  export WORK_ROOT=/absolute/research-work
  export SOAK_BASELINE_FILE=/private/outside-work-root/soak-baseline.txt
  DB="$WORK_ROOT/research.sqlite"
  IFS= read -r SOAK_BEFORE < "$SOAK_BASELINE_FILE"
  case "$SOAK_BEFORE" in ''|*[!0-9]*) echo "invalid soak baseline" >&2; exit 2;; esac
  if ! SOAK_AFTER="$(sqlite3 -readonly "$DB" \
    "SELECT count(*) FROM cycle WHERE status='done' AND route IS NOT NULL;")"; then
    echo "soak final query failed" >&2
    exit 2
  fi
  case "$SOAK_AFTER" in ''|*[!0-9]*) echo "invalid soak final count" >&2; exit 2;; esac
  test "$((SOAK_AFTER - SOAK_BEFORE))" -ge 200
  ```

  `--max-cycles 200` 只是本次调用上限，不是通过证明：τ、pause、文件请求、global stop 或既有 terminate 都可能让
  入口以 0 退出但不足 200 轮。真实 soak 必须记录启动前后 `cycle.status='done' AND route IS NOT NULL` 的差值并
  要求 `>=200`，同时保留真实 provider/import/train/fault 回执；不能只看进程退出码或 `MAX(cycle.id)`。

  在启动目标 execution 前准备一个**新的** 32 位小写十六进制 `schedule_id`。下面的 `execution_kind` 与
  `(db_owner_kind, db_owner_id)` 必须和将出现的 receipt 精确一致；示例在 run row 41 的 manifest train 上杀
  payload。生成器保证 sorted compact JSON、单个结尾换行和 0600 权限：

  ```bash
  export FAULT_SCHEDULE=/absolute/fault-schedule.json
  python - <<'PY'
  import json, os, secrets
  from pathlib import Path

  value = {
      "version": 1,
      "protocol": "meta-research-fault-schedule/v1",
      "schedule_id": secrets.token_hex(16),
      "work_root": os.environ["WORK_ROOT"],
      "event_timeout_s": 3600,
      "events": [{
          "event_id": "kill_train_41",
          "action": "kill_execution_payload",
          "execution_kind": "manifest-train",
          "db_owner_kind": "run",
          "db_owner_id": 41,
      }],
  }
  path = Path(os.environ["FAULT_SCHEDULE"])
  path.write_bytes((json.dumps(
      value, ensure_ascii=False, sort_keys=True,
      separators=(",", ":"), allow_nan=False) + "\n").encode())
  path.chmod(0o600)
  PY
  ```

  selector 已预知时，Terminal B 先执行 `validate` 和 `run`（runner 会等待），再放行 Terminal A 进入目标
  外调；`run` 返回后执行 `verify`：

  ```bash
  python -m orchestrator.fault_schedule validate --schedule "$FAULT_SCHEDULE"
  python -m orchestrator.fault_schedule run --schedule "$FAULT_SCHEDULE"
  python -m orchestrator.fault_schedule verify --schedule "$FAULT_SCHEDULE"
  ```

  若 DB owner ID 只能在外调开始时分配，则先启动 Terminal A，从新出现的 durable running receipt 读取
  `kind` 与 `context.db_owner_kind/db_owner_id`，立即冻结 schedule 并启动 Terminal B；这种方式只适用于足够长的
  调用，短调用可能在 spend 前已 terminal。running receipt 在 guardian 放开 exec gate 前已经耐久发布，故
  `kill_execution_payload` 只证明杀死了该 supervised payload identity，不机械证明科学程序已经开始有效工作。

  schedule 路径与 `work_root` 都须为当前 UID 持有的规范绝对路径；sidecar 必须和 owner 位于同一 host、boot、
  PID namespace 与 UID，且内核支持 pidfd。selector 三元组在 `state/executions/` 全历史必须唯一。`kill_owner`
  后的重启由 Terminal A/systemd 负责；多事件 schedule 不应依赖 `--once`。全 work-root 同时只允许一个 runner，
  状态位于 `state/fault-schedules/<schedule_id>/`；新实验不得复用旧 ID。

  `event_timeout_s` 分别约束 trigger、target exit 与 guardian aftermath 等等待段，不是整个 event 的总 wall-clock
  deadline；目标 GPFS 较慢时应为各段保留余量。

  `spent` 已发布就绝不重发 signal；`applied` 仅表示内核接受了对 pinned task 的 SIGKILL。退出码 0 表示
  valid/complete，2 表示 incomplete/inconclusive，3 表示 failed/unsafe，130 表示操作者中断。即使 complete，
  receipt 仍明确写 `signal_exactly_once=false` 与 `recovery_verified=false`：该 runner 验故障后果，不代替下一
  检查点的 restore/续跑验收，也不证明基础设施 STONITH。
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
- strict 单 quest/admin 模式的 Web capability 用于隔离其他本机 OS 用户、跨站浏览器请求和无 token 的本机
  客户端；同 UID 恶意进程仍可能取得进程私有凭证，同源 XSS 也可读取 sessionStorage。默认多任务产品模式则按
  第一版单用户本机安装假设，把同机 OS 用户/本机客户端纳入信任边界，以支持裸 loopback 地址自动引导；它仍用
  Bearer、同源检查和 sessionStorage 抵御跨站控制，但不能声称隔离同机其他用户。普通用户日常重开直接访问 Web，
  不读取或修改后端状态文件；凭证疑似泄漏时应停止服务并由部署维护入口轮换。两种模式都不替代 loopback/SSH
  tunnel 边界。
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
- 已支持 build / exec/eval target；默认 `build_system()` 已装配同 owner fenced `ImportWorker` 和
  `dependency_wait` 恢复。正常 Plan 由一个 resident 主 Codex 在当前 turn 内启动干净子智能体审查；当类型门
  确认需引入新外部 baseline 家族且当轮无候选时，主 Codex 调用受 schema 限制的 `plan_import_search` MCP，
  读取返回的托管 ContextPack 索引后继续形成最终 `plan.json`，不会交 sidecar 或重开顶层上下文。受信 host
  GitHub REST connector 在 DB 事务外做只读检索，回执完整后用一个短事务原子登记 pinned commit、搜索快照、冻结
  license evidence/SPDX、机械 auto/review 裁定和完成 marker，然后重渲染 plan 四锚。回执已落、DB 未提交的
  崩溃可不重搜直接续提交；无回执的中断只会重复幂等只读 GET，不会重复登记。
- production 的 build/exec/import/eval 共用同一个 owner-fenced `PoolPublisher`：checkpoint、代码、配置、
  protocol 与评估 attempt 先复制到 `baselines/`、`protocols/`，再发布 `pool/manifests/<sha256>.json`；DB
  checkpoint 只登记相对正式路径/hash。训练 decision 与 checkpoint 同事务绑定，评估 attempt、metrics、
  execution log、完整 publication decision 与 cards 同一 gate 事务闭合后，baseline/variant 才能进入
  `legal`。崩溃最多留下可由同 hash 重放收养的未引用正式文件，不会留下指向 cycle staging 的 legal 身份；
  selector 还会拒绝只有 `status=legal`、没有完整 publication closure 的旧行。
- Bundle 每个 cycle 只有一个 resident 主 Codex turn。它通过 `bundle_next_target` 串行绑定目标，通过
  `bundle_execute` 异步启动官方 smoke/train/eval，并用 `bundle_status` 持续读取部分日志；工程错误经
  `bundle_repair` 在原上下文内修复重跑。当前 `flow.retry.bundle_repair=null`，不创建 fresh session 或按轮次
  截断；只有权威反馈证明冻结 plan 本身不可执行时才调用 `bundle_replan`，随后仍必须进入 Reasoning。
- 三个非默认触发也已接入，但它们不能冒充 `new_structure`：`human_named` 只接受经 hard confirmation 的
  结构化 `inject_question`（规范 GitHub URI + 可选精确 commit + need summary），plan 只能回引其 exact authority
  hash，受信 connector 再固定 revision/license；自由文本 URL 不形成 authority。控制台消费和外部普查完成都
  只冻结 `request_ref`，不旁路写 question；reasoning 必须逐字段复制 ref/text/parent/kind 并给出显式证据关闭
  predicate，随后由唯一的 SQLite StateStore 在建题同一事务里核 request provenance、写 admission/binding，且仅在
  对应路径生成 human/reference authority。`stuck` 只有在 policy 的
  visit/consecutive-inconclusive 双阈值命中时才做一次只读普查：visit 是终身防贪心计数，连续失败由
  goal-version scoped append-only decision 独立记账（goal amend 后从零开始）。结果只派生新参照问题，原问题只挂 question
  dependency，且 authority 与 origin→child pending dependency 随 question INSERT 原子落库，永不直接登记候选或
  `import_defer`。`sota_reference` 先从 policy host allowlist 做有界 HTTPS
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
- CP11.3c 的 120 轮是无真实 provider 工作负载的控制面/投影回归；尚未完成跨节点 VEPFS crash-stop 接管实机竞态，
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
└── runtime/                 # 本机部署的持久数据根（整体不进入 Git）
    ├── quests/<quest-id>/   # 每个任务独立的 DB、cycles、实验、日志、input 与 state
    ├── state/               # 跨任务创建、Web、本机来源与发布状态
    └── self-test/           # 本机验收脚本的隔离产物
```
