# meta-research 元循环系统 —— 运维操作手册

自主研究编排器：一条命令让真 Codex 全自动跑「出题 → 分解 → 建基线/变体 → 训练 → 评估 → 双评审 →
关问」的研究元循环。**确定性编排器从不推理**（Codex 是无状态阶段工人，只产合 schema 的产物，永不碰
数据库）；SQLite 是唯一真相（36 表冻结 DDL + 三重锁）；真执行由 harness 跑真子进程；崩溃 kill-9 可
从 DB 无半写恢复。

> 设计真相唯一在 `../reference/第一部分-系统架构设计.md`；本 README 是**操作面**（怎么跑、怎么配、怎么
> 观测/干预、边界在哪）。构建历史见仓库根 `ROADMAP.md` / `build_log/`。

## 0. 一分钟跑起来

```bash
cd meta-research
python -m pip install -r requirements-dev.txt          # pytest / jsonschema / PyYAML（+真 Codex 见下）
python -m pytest tests/ -q                              # 自验：以当前测试输出为准

# 全自动跑（需真 Codex CLI + 代理，见 §2）：
export HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890
python -m orchestrator.run --system-root . --work-root /path/to/run_dir --max-cycles 50
```

- `--system-root`：含 `input/goal_brief.md`、`policies/`、`prompts/`、`schemas/` 的仓库根（一般就是 `.`）。
- `--work-root`：本次运行的产物根（`research.sqlite` / `cycles/` / `state/` 落这里）。**重启用同一个
  work-root 即从断点续跑**（DB 权威，非进程内记忆）。
- `--max-cycles`：本次最多推进轮数（安全上限，默认 100，与系统自终止并存）。
- 默认入口在人工 pause 或文件请求阻断时**保持单写进程常驻**，周期消费控制台 spool、扫描提醒，解除阻断后
  自动从同一 DB 游标继续；批处理/调试若希望遇阻即返回，显式加 `--once`。`--poll-interval-s` 可调等待轮询
  间隔（默认 1 秒，最小 0.01 秒）。

停机时打印 `[run] dual_mode=… 推进 N 轮：[…]；停因=…`。停因见 §6。

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
- `execution`：manifest 命令围栏——`default_timeout_s` / `max_timeout_s` / `path_allowlist`（允许 argv
  引用的 work_root 以外绝对路径前缀，如真数据根）。
- `session.dual_mode`：A=一 turn 一阶段（默认；A/B 实测定默认属运维）。

> **改 policy.yaml 是决策性变更**（影响研究语义/门禁/预算）——生产改动应走评审。改后 `pytest
> tests/test_schemas.py` 会校验它仍合 `schemas/policy.schema.json`。

## 4. 观测与人工干预（跑起来之后）

- **状态卡**：`<work-root>/state/status_card.json`——每阶段边界原子发布的人可读快照（当前轮/问题/进度）。
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

当前是**运维金丝雀态**，足以让真 Codex 端到端跑通并验证研究链路，但正式跑数百轮真研究前仍须硬化：

- 同一 work-root 目前必须由运维保证只启动一个 `orchestrator.run`；SQLite 事务锁不能阻止两个进程在事务外
  重复调用 Runner。进程级非阻塞 instance lease 属 CP11.3，在它完成前不得并行启动第二个 run 进程。
- 当前执行超时只可靠终止直接子进程，主动脱离/派生的孙进程可能继续存活；整组 session teardown 与 timeout
  终态收敛属 CP11.3。完成前只能运行受信任、不会自行 daemonize 的 manifest 命令。
- plan 的 `critical/budget_estimate` 权威落库/早退仍待 CP11.3；当前金丝雀运行不要依赖“非关键 target
  失败后继续”。动态 `goal_amend` 的专用路由、不可变升版、reasoning/status 按 cycle goal version 隔离与
  applicability 恢复已由 CP11.2b.3b 闭合。
- 代码物化在编排器管理的 staging（净土物化 + sha256 哈希对账 + argv-only 禁 shell + 路径/env 围栏），
  **但真 git worktree 隔离 + env lock 强校验属后续硬化步**。
- 用户文件已有 DB 终态授权、ContextPack ref 白名单、hash/size 复验和稳定只读 fd；非 bundle 阶段只看
  有界 UTF-8 预览。**这不是机械 prompt-injection/恶意代码隔离**：当前 Codex 仍可使用只读工具，bundle
  代码仍由主机 harness 执行。CP11.4 容器/VM 隔离完成前，只能接纳操作者信任的文件或人工审过的文本摘要，
  不得把任意第三方附件当作对抗性安全输入。
- Web capability 用于隔离其他本机 OS 用户、跨站浏览器请求和无 token 的本机客户端；同 UID 恶意进程仍可
  读取 0600 token，同源 XSS 也可读取 sessionStorage。轮换时先停 console server，删除
  `<work-root>/state/.console-capability`，再重启并用新 fragment 打开；它不替代 loopback/SSH tunnel 边界。
- **成本护栏已转活，但不是供应商账单**：`ledger.money` 用 Codex CLI 回报的总 token 与本地
  `price_per_1k_tokens` 折算，用于运行护栏和趋势观测。如果编排器在外部调用已完成、但用量落库前被
  `SIGKILL`，该次用量仍可漏记；生产级精确账单需再加「调用前意图 + 持久回执 + 幂等补账」协议。
- 已支持 build / exec target；**eval target（免训练评估）与 import（外部基线导入）+ route dependency_wait
  特化 = 后续检查点（CP8.6b）**——遇到它们系统会干净业务拒、不楔死。
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
