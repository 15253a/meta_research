# Meta Research vNext

Meta Research 是一个本地优先、人机协作的科研执行系统。它把研究目标、问题树、Idea、Plan、Bundle、Reasoning、实验、证据、HumanRequest 和 Writing 放进同一个可审计工作空间，并通过公开 Web 界面完成主要观察、控制和 receipt 下钻。

`test-all` 是当前用于真实部署测试的整合分支。它适合在受控环境里跑完整科研流程、验证真实 Provider 和实验执行；当前还不是面向公网的多用户 SaaS。

## 先看结论

- 产品入口是 `meta-research` CLI 和它启动的 Lumen Web，不是 `scripts/verify`。
- 服务只监听本机回环地址 `127.0.0.1` 或 `::1`；不要把它直接暴露到局域网或公网。
- 首选测试环境是 Ubuntu 22.04/24.04 x86_64，并且必须有可用的 `systemd/logind`；Windows 11 + WSL2 属于目标平台，但同一候选目前还没有完成 Ubuntu/WSL2 双平台最终验收。
- 普通 UI、持久化、重启和浏览器流程可以先测；真实 Codex/Claude 工作和实验执行还要求 Provider 登录、GPU 探测以及电源抑制能力全部就绪。
- 每个测试者应使用独立的数据目录。不要让两个 daemon 同时读写同一个 data root。

## 产品里有什么

一次研究以一个 **Quest** 为边界：

1. **Research Brief**：在同一创建窗口中填写背景、目标与边界；可选材料在后台接纳，成功后自动绑定精确版本。
2. **Question Tree**：正式问题、分支、生命周期、证据和历史。
3. **Cycle**：围绕一个当前 Question 推进的一轮研究。
4. **Idea → Plan → Bundle → Reasoning**：四个正式 Stage。
5. **Experiment**：受控执行与 stdout 观察。
6. **Human Collaboration**：Companion 对话和需要人类处理的 HumanRequest。
7. **Writing**：基于冻结研究快照生成报告、论文、演示文稿，并安全交付到本地文件。

系统刻意区分几件容易混淆的事：执行完成不等于研究资产已接受，资产已接受不等于 Formal Measurement 已接受，Formal Measurement 已接受也不等于 Stage 已推进。Web 会分别显示这些事实和对应 receipt。

## 运行要求

### 基础环境

- Git
- Python 3.11–3.13；推荐 Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js 22 与 npm（安装锁定的部署专用 Codex CLI、运行完整自动化测试时需要）
- Google Chrome，用于当前受审 Web 流程
- 足够的本地磁盘空间；研究数据库、对象、日志和 Provider spool 都保存在 data root

### 跑真实研究还需要

- 部署目录内的专用 `codex` CLI，版本与仓库锁定版本一致，并已在部署专用 `CODEX_HOME` 中由启动 daemon 的同一 OS 用户登录
- `claude` CLI，版本与仓库锁定版本一致，并已由同一 OS 用户登录；首次 Quest 的 Codex 草拟不依赖 Claude，但完整双 Provider conformance 依赖它
- `nvidia-smi` 可用，以形成真实 Resource Envelope
- Ubuntu 上可工作的 `systemd-inhibit` 与 system bus/logind
- WSL2 上可调用的 `powershell.exe` guardian
- Provider 账号具备所选模型的权限、额度和网络访问

当前锁定版本会出现在 `meta-research doctor --json` 的 `adapters[].locked_version` 中，不要只以“命令能运行”代替 conformance。

当前 `test-all` 基线锁定 Codex CLI `0.147.0` 和 Claude Code `2.1.220`。分支更新后以新安装的 `doctor` 输出为准。

### 非 systemd 开发机

生产默认会在原生电源抑制能力无法确认时 fail closed。对于由操作者保证持续在线、
并明确接受进程不会阻止休眠或关机的本地开发环境，可以在启动 daemon 前设置：

```bash
export META_RESEARCH_ASSUME_ALWAYS_ON=1
```

此时运行态证据中的电源后端会明确显示为
`operator_attested_always_on`，不会伪装成 logind 或 Windows guardian。该开关只绕过
宿主电源管理要求；未设置时仍使用原有生产保护路径。

## 隔离部署与启动

目前 `test-all` 通过源码安装，还没有 PyPI 稳定发行版。

下面把 checkout、Python 环境、Provider CLI、研究数据和 Codex session 全部放到同一个**非系统盘**部署根目录。不要把 `DEPLOY_ROOT` 设为 `/root`、`$HOME`、Git checkout 本身或主盘上的临时目录；创建目录后用 `findmnt -T "$DEPLOY_ROOT"` 确认它实际落在哪个挂载点。

```bash
export DEPLOY_ROOT=/absolute/path/on/non-system-disk/meta-research-test-all
export META_RESEARCH_DATA_ROOT="$DEPLOY_ROOT/runtime/meta-research"
export CODEX_HOME="$META_RESEARCH_DATA_ROOT/provider-homes/codex"
export CODEX_SQLITE_HOME="$CODEX_HOME"
export CODEX_INSTALL_ROOT="$META_RESEARCH_DATA_ROOT/provider-tools/codex-cli"
export UV_CACHE_DIR="$DEPLOY_ROOT/cache/uv"
export npm_config_cache="$DEPLOY_ROOT/cache/npm"

umask 077
install -d -m 700 \
  "$DEPLOY_ROOT" "$DEPLOY_ROOT/app" "$DEPLOY_ROOT/runtime" \
  "$DEPLOY_ROOT/tools" "$DEPLOY_ROOT/cache"

export APP_ROOT="$DEPLOY_ROOT/app/meta-research"
git clone --branch test-all --single-branch \
  git@github.com:15253a/meta_research.git "$APP_ROOT"

cd "$APP_ROOT"
test "$(git branch --show-current)" = test-all
git rev-parse HEAD

uv sync --locked --no-dev --python 3.12

# init 必须先看到一个空的 Meta Research data root。
./.venv/bin/meta-research init --data-root "$META_RESEARCH_DATA_ROOT" --json

# 按当前源码声明的锁定版本把 Codex 单独安装到部署目录，
# 不使用系统或账号包装器。
install -d -m 700 "$CODEX_HOME" "$CODEX_INSTALL_ROOT" "$npm_config_cache"
npm --prefix "$CODEX_INSTALL_ROOT" init --yes >/dev/null
CODEX_LOCKED_VERSION=$(./.venv/bin/python -c \
  'from meta_research.harness_adapters import CODEX_LOCKED_VERSION; print(CODEX_LOCKED_VERSION)')
npm --prefix "$CODEX_INSTALL_ROOT" install --save-exact \
  "@openai/codex@$CODEX_LOCKED_VERSION"
export PATH="$CODEX_INSTALL_ROOT/node_modules/.bin:$PATH"
export CODEX_BIN="$CODEX_INSTALL_ROOT/node_modules/.bin/codex"
hash -r

# 以下检查都必须成功；CODEX_HOME 不能是软链接，也必须仍位于 DEPLOY_ROOT 下。
test -x "$CODEX_BIN"
test ! -L "$CODEX_HOME"
case "$(readlink -f "$CODEX_HOME")/" in "$DEPLOY_ROOT"/*) ;; *) exit 1 ;; esac
test "$(command -v codex)" = "$CODEX_BIN"
"$CODEX_BIN" --version

# 只查询部署专用 home 的登录状态；若未登录，再执行下一行的 device login。
env CODEX_HOME="$CODEX_HOME" CODEX_SQLITE_HOME="$CODEX_HOME" \
  "$CODEX_BIN" login status
# env CODEX_HOME="$CODEX_HOME" CODEX_SQLITE_HOME="$CODEX_HOME" \
#   "$CODEX_BIN" login --device-auth

./.venv/bin/meta-research version --json
./.venv/bin/meta-research launch --data-root "$META_RESEARCH_DATA_ROOT"
```

不要复制或软链接现有 `~/.codex`、`/root/.codex-openai-account` 或其中的 `auth.json`。新的 home 需要单独登录；这样旧 session、skills、配置和凭据不会混入本次产品测试。当前锁定的 Codex 没有独立的 session-directory 开关，活跃与归档记录分别固定写到 `$CODEX_HOME/sessions/` 和 `$CODEX_HOME/archived_sessions/`，所以必须移动整个 `CODEX_HOME`，不能只改日志目录。

产品固定从 data root 内的上述绝对路径调用 Codex，不会回退到 `PATH` 中的全局 `codex`；把 `.bin` 放在 `PATH` 最前面是为了让人工版本检查也指向同一安装。每个新 shell 仍应重新导出这些变量，所有产品命令也应显式传入 `--data-root`。尤其不要从会自行覆盖 `CODEX_HOME` 的账号 wrapper 登录或检查版本。

`launch` 会确保 daemon 已启动、签发一次性浏览器 grant，并打开 Lumen Web。第一次启动会自动把数据库迁移到当前 schema。

clone 私有仓库需要相应的 GitHub 访问权限。建议把 `git rev-parse HEAD` 的完整输出记入测试记录，以便问题能够复现。

data root 应放在 Git checkout 之外。当前分支对所有有状态 CLI 都 fail closed：既没有 `--data-root`、也没有 `META_RESEARCH_DATA_ROOT` 时会直接拒绝，相对路径也会拒绝，不再静默按当前目录或用户 home 回退到主盘。仍建议每条命令显式传 `--data-root`，并在首次 Provider 请求前用 `findmnt` 核对真实挂载点。

如果机器不能自动打开浏览器：

```bash
./.venv/bin/meta-research launch \
  --data-root "$META_RESEARCH_DATA_ROOT" --no-browser --json
```

命令会返回一个本地 `file://` 启动页。请在 30 秒内打开它；不要复制或分享其中的一次性 grant。session 建立或 grant 过期后，删除命令返回的精确 `browser-launch-*.html` 文件。直接访问 `http://127.0.0.1:8765` 得到 `401` 是正常的，正确入口始终是 `meta-research launch`。Web session 最长为 12 小时；过期后重新 launch。

启动后检查：

```bash
./.venv/bin/meta-research status \
  --data-root "$META_RESEARCH_DATA_ROOT" --json
./.venv/bin/meta-research doctor \
  --data-root "$META_RESEARCH_DATA_ROOT" --json
```

`status` 回答 daemon 是否存活；`doctor` 检查 Harness/MCP、Provider 适配器和运行时电源保护。GPU 不在 `doctor` 的探测范围内，要在 Quest Creation 的 compute probe 单独确认。界面能打开但 `doctor` 仍为 unavailable，通常表示 Provider、电源抑制或 conformance 尚未完成。

在发起第一笔真实 Provider 请求前，可以在 Ubuntu/WSL2 上核对 daemon 的实际绑定；下面只打印两个存储变量，不要输出完整进程环境：

```bash
DAEMON_PID=$(./.venv/bin/meta-research status \
  --data-root "$META_RESEARCH_DATA_ROOT" --json | \
  ./.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["pid"])')
tr '\0' '\n' <"/proc/$DAEMON_PID/environ" | \
  grep -E '^(CODEX_HOME|CODEX_SQLITE_HOME)='
findmnt -T "$CODEX_HOME"
```

两项变量都应等于 `$META_RESEARCH_DATA_ROOT/provider-homes/codex`，`findmnt` 应显示部署数据盘。完成一次真实 Codex conformance、Quest 草拟或 Stage 工作后，再确认新 JSONL 只出现在部署 home：

```bash
find "$CODEX_HOME/sessions" "$CODEX_HOME/archived_sessions" \
  -type f -name '*.jsonl' -print 2>/dev/null
```

## 第一次实际使用

### 1. 创建 Quest

在左侧 rail 点击 `+`，进入 Quest Creation：

- 用“背景、目标、边界”三个研究语言字段写出 Research Brief；目标与边界必填。
- 如有本地材料，直接在同一窗口选择文件或文件夹。名称、媒体类型和保管方式由系统推导，Research Memory 在后台接纳，成功后自动绑定精确 AssetVersion。
- 可选材料仍在处理或处理失败时，可以继续编辑并走 `direct`；`provided_only` 仍只接受已经形成 RM receipt 的材料。
- 选择时间预算和研究配置。
- 第一次测试建议选 `direct` 文献路线；DeepFetch 需要额外的外部访问能力。
- 执行 compute probe，检查检测到的设备并形成 Resource Envelope。
- 让 Codex 起草第一条 Formal Question。
- Proposal Drafter 只接纳锁定的 Codex CLI `0.147.0`，并在独立 `research-workspace` 中形成 schema 输出。部署使用随 wheel 打包且校验哈希的单模型目录禁用 shell 与 `apply_patch`，同时显式禁用用户配置、MCP、Web、全部内置本地工具、agent、apps/plugins/memories、Skill 指令与环境继承。Quest/Literature 输入被标记为“不可信研究数据而非指令”，UTF-8 prompt 在启动 effect 前受硬上限约束；宿主不支持 user namespace 时使用的 `danger-full-access` 不会扩大成 Quest 授权。
- 审阅六字段 Proposal、Impact Preview 和精确 DraftRevision，再确认创建。

关闭创建窗口不会删除 durable draft；发生并发写冲突时，界面会保留可恢复信息，不会用旧的浏览器值覆盖新版本。

### 2. 观察四个 Stage

Quest 接受后，系统按当前 Question/Cycle 推进：

- **Idea**：形成、评估并接受 IdeaSet。
- **Plan**：形成与接受 Formal Plan。
- **Bundle**：建立 Target DAG、执行 TargetRun，并以 Completed、Skipped 或 Exhausted 诚实收口。
- **Reasoning**：组合正式证据，给出结论或提出后继研究方向。

首页显示低密度返场摘要；需要细节时再展开卡片、receipt 或 stdout。Stage 卡片中的 unavailable、stale、waiting 和 exhausted 都是正式状态，不应当被当成普通报错或用占位数据绕过。

### 3. 使用 Question Tree

从当前研究空间打开问题树后可以：

- 在 800/1440 宽度查看画布，在 390 宽度查看缩进大纲。
- 选择问题并把只读 Question context 同步到右侧 Companion。
- 查看该问题的 lifecycle history 和已验证 evidence。
- 查看关联 HumanRequest、Cycle binding 和 Advancement Engine foreground。
- 重新打开当前或历史 Experiment 的 stdout。

单纯选择、浏览和展开 receipt 不会产生 Owner 写入。

### 4. 与 Companion 协作

在 Question Tree 选择一个问题，再点“与 Companion 讨论此题”。发送的消息会绑定精确的 Quest、Question、content hash 和 lifecycle revision。若问题在发送前已经被 prune、restore 或修订，系统会以 typed stale 状态拒绝旧上下文，而不会静默重绑到另一个问题。

HumanRequest 有四类常见表面：

- Quest 级请求：需要人类决定，优先于自动 stdout 弹层。
- 当前实验局部请求：与当前运行紧密相关。
- Research Asset 请求：需要补资料、确认或恢复资产。
- Writing 请求：与报告、论文、PPT 或交付有关。

### 5. 运行实验

正式科研执行由 Bundle Target/TargetRun 通过 Harness 完成：根 Agent 在受权的专属 workspace 内编写代码、运行真实训练与评估、检查结果并迭代。Execution Observer 会显示当前 Fence 的 stdout、attempt generation、session 和 freshness；关闭后仍可从首页或 Question Tree 重新打开。

当前内置 micro-experiment 是系统受控的、可复现实验通路，用来验证完整执行与计量链；它不是任意 shell，也不是通用 GPU 作业调度器。

入口位于 Idea 卡片的“启动微型真实实验”。展开“填写实验意图”，选择 `retrain` 或已有结果上的 `remeasure`，填写标题、假设、Variant parameter 和 Sample count，再点“启动实验”。它会在当前 data root 写入真实且持久的 RM/RG/AR 事实，同一 Quest 的终态结果还可能进入 Writing 冻结快照。

因此，micro-experiment 只能在专用的 smoke data root 中验证；不要在正式科研 data root 中启动它，也不要把跑过 micro 的数据库复用为正式实验数据库。复用旧 data root 时，未完成的 micro Run 可能在 daemon 启动后继续恢复。结果需要分别观察 execution、RM asset 和 RG Formal Measurement；它不会自动推进正式 Stage。

### 6. 生成 Writing 产物

Writing 是独立的用户触发工作流，不是第五个研究 Stage。它基于授权时冻结的研究快照生成：

- Report
- Paper（DOCX）
- Presentation（PPTX）

后续研究进度不会偷偷改写已经冻结的 Writing 输入；交付确认也有独立 receipt 和恢复路径。当前 production registry 的交付能力只是在已验证目录中**新建本地文件**，不会覆盖已有文件，也不是邮件、云盘或公网发布。

## 启用真实 Provider

先确认两个 CLI 都能在同一个 shell、同一个 OS 用户下正常工作并已登录，再启动 daemon。不要把账号 token 写进仓库或 README。如果是在 daemon 已启动后才安装 CLI、登录或修改 `PATH`，先 `meta-research stop`，再重新 `meta-research launch`，让新 daemon 继承正确环境。

启动完整 Harness conformance：

```bash
./.venv/bin/meta-research conformance start \
  --data-root "$META_RESEARCH_DATA_ROOT" \
  --codex-model 'gpt-5.6-sol' \
  --claude-model '<your-account-model-id>' \
  --json
```

Codex production 路径固定使用 `gpt-5.6-sol`，reasoning effort 固定为 `max`；CLI 省略 `--codex-model` 时也会采用这一不可降级的默认值。

然后反复查看公开状态，直到完成或出现明确 blocker：

```bash
./.venv/bin/meta-research doctor \
  --data-root "$META_RESEARCH_DATA_ROOT" --json
```

Conformance 会产生真实 Provider 请求，可能计费。它检查的是当前安装、当前登录和当前模型组合，不能由 CI 中的 fake executable 代替。

全新 `CODEX_HOME` 不会带入旧账号 home 的 Skill、plugin 或 hook。完整 conformance 还要求受审 Harness profile（包括独立子 agent、Skill 与 hook evidence）；当前仓库没有把这些账号级资产打包成安装器，因此干净 home 可能诚实返回 typed unavailable。不要为了让 gate 变绿而复制整个旧 Codex home。

## 日常运维

### 状态、日志和停止

```bash
./.venv/bin/meta-research status \
  --data-root "$META_RESEARCH_DATA_ROOT" --json
./.venv/bin/meta-research doctor \
  --data-root "$META_RESEARCH_DATA_ROOT" --json
./.venv/bin/meta-research stop \
  --data-root "$META_RESEARCH_DATA_ROOT" --json
```

主要数据位于 `$META_RESEARCH_DATA_ROOT`：

```text
data-root.json
meta-research.sqlite3
objects/sha256/
run/
logs/daemon.jsonl
provider-homes/codex/{auth.json,sessions/,archived_sessions/,log/}
provider-tools/codex-cli/
```

`stop` 只停止 daemon，不删除研究数据。故障排查优先看 `status --json`、`doctor --json` 和 `logs/daemon.jsonl`，不要直接修改 SQLite 或 Owner spool。

`start --json` 和 `session` 会把一次性 bootstrap token 输出到终端。不要把这类输出贴到公开 issue、聊天或共享日志；普通使用优先选择 `launch`。

### 备份

备份必须包含整个 data root，而不只是 SQLite：

```bash
./.venv/bin/meta-research stop --data-root "$META_RESEARCH_DATA_ROOT"
cp -a "$META_RESEARCH_DATA_ROOT" /absolute/secure/backup/location/
```

data root 含控制密钥、会话材料和 Provider transport spool，备份目录应保持私有。恢复时确保没有另一个 daemon 正在使用原目录或恢复目录。

### 更新 `test-all`

```bash
./.venv/bin/meta-research stop --data-root "$META_RESEARCH_DATA_ROOT"

git fetch origin
git switch test-all
git pull --ff-only origin test-all
uv sync --locked --no-dev --python 3.12
CODEX_LOCKED_VERSION=$(./.venv/bin/python -c \
  'from meta_research.harness_adapters import CODEX_LOCKED_VERSION; print(CODEX_LOCKED_VERSION)')
npm --prefix "$CODEX_INSTALL_ROOT" install --save-exact \
  "@openai/codex@$CODEX_LOCKED_VERSION"

./.venv/bin/meta-research launch --data-root "$META_RESEARCH_DATA_ROOT"
```

更新前先备份整个 data root。新版本在启动时自动执行迁移；目前还没有正式的一键回滚工具。

### 给其他人测试

推荐每位测试者：

1. 各自 clone `test-all`。
2. 各自建立 Python 环境。
3. 各自使用独立的绝对 data root。
4. 各自登录自己的 Codex/Claude 账号。
5. 在自己的 Ubuntu/WSL2 主机上通过 `meta-research launch` 使用。

当前版本不是中心化多用户服务器。不要共享 bootstrap token、browser grant、data root 或 Provider 凭据，也不要用反向代理把端口公开。

### 在无桌面的远程 Ubuntu 主机测试

首选方案仍是直接在测试者自己的桌面 Ubuntu/WSL2 上运行。必须使用远程主机时，可以通过 SSH 保留 loopback 边界：

```bash
# 例：远端 daemon 实际监听 8765；在测试者自己的工作站运行并保持会话开启
ssh -N -L 8765:127.0.0.1:8765 tester@example-host
```

先用远端 `meta-research status --json` 或 `launch` 返回的 `target_url` 确认 daemon 的实际端口；SSH 转发右侧端口必须与它一致。然后在远程主机执行 `meta-research launch --no-browser --json`，通过受保护的 SSH 通道把返回的 0600 `browser-launch-*.html` 复制给同一位测试者，在 30 秒内打开一次；session 建立后删除返回的精确远端文件和本地副本。不要把 HTML 或其中的 grant 发给第二个人。浏览器会通过本机 `127.0.0.1:8765` 进入 SSH 隧道。

这只是单用户远程测试方式，不是多用户托管方案；远端端口仍应只绑定 `127.0.0.1`。

## 常见问题

### `power_inhibitor_systemd_reconciliation_required`

系统没有拿到可信的长运行电源保护。在 Ubuntu 上检查：

```bash
systemd-inhibit --list
```

如果出现 `Failed to connect to bus: No such file or directory`，说明当前容器/主机没有可用的 systemd/logind bus。请换到支持的完整 Ubuntu 主机或修复宿主服务；不要伪造 `systemd-inhibit` 来跑真实研究。

### Web 能开，但不能起草或推进

依次检查：

1. `meta-research doctor --json`
2. `codex --version` 与登录状态
3. `claude --version` 与登录状态
4. daemon 的 `PATH` 是否能找到两个 CLI 和 `nvidia-smi`
5. Harness conformance 是否完成
6. `logs/daemon.jsonl` 中的 typed blocker

### 浏览器没自动打开或登录失效

重新执行：

```bash
./.venv/bin/meta-research launch \
  --data-root "$META_RESEARCH_DATA_ROOT" --no-browser --json
```

使用新的启动页；旧 grant 会过期或被消费。不要直接复用旧 `browser-launch-*.html`。

### 端口被占用

改用另一个回环端口：

```bash
./.venv/bin/meta-research launch \
  --data-root "$META_RESEARCH_DATA_ROOT" --host 127.0.0.1 --port 8876
```

如果已有 daemon 正在运行，先用 `status` 查看它实际绑定的端口；需要改端口时先 `stop`。

### data root 报错

`init` 只接受空目录或已有合法 `data-root.json` 的 Meta Research 目录。不要把普通工作目录、Git 根目录或另一个应用的数据目录直接当作 data root。

## 正式产品命令与测试夹具

正式入口只有这些公开 CLI：

```text
meta-research version
meta-research init
meta-research start
meta-research status
meta-research doctor
meta-research conformance start
meta-research session
meta-research launch
meta-research stop
```

`./scripts/verify` 是安装/CI 验证脚本。它会创建临时环境，并可能注入 fake `codex`、`nvidia-smi`、`systemd-inhibit` 和确定性 Provider；结束后还会清理临时数据。它不能用于部署，也不能证明某台机器具备真实研究能力。

一次跑完确定性自动化全套：

```bash
TMPDIR=/dev/shm ./scripts/test-all
```

它依次运行锁文件/环境校验、全部 Python 测试、Web 类型检查与构建、真实 Chrome Playwright E2E，以及安装后公开产品 smoke。该命令不使用真实 Provider，也不能代替下一节的宿主/Provider gate 和人工端到端验收。

需要分层定位问题时，可分别运行：

```bash
# Python
uv sync --locked
TMPDIR=/dev/shm uv run pytest -p no:cacheprovider

# Web build、应用/E2E 类型检查与真实 Chrome E2E
cd web
npm ci
npm run build
env -u FIXED_REFERENCE_CALIBRATE \
    -u FIXED_REFERENCE_CAPTURE_PENDING \
    META_RESEARCH_CHROME=/usr/bin/google-chrome \
    ./node_modules/.bin/playwright test --workers=1
```

不要设置 fixed-reference 校准环境变量来“更新”受审截图，也不要用提高像素容差掩盖视觉偏差。

Web 构建/测试推荐 Node 22。受审像素证据使用 Google Chrome `151.0.7922.71`；其他 Chrome 版本可以做功能探索，但其截图不能直接作为固定视觉验收证据。

## 建议的实际验收顺序

不要一上来就用最长的研究任务。完整验收使用两个互不复用的部署根：按前面的安装步骤分别建立 `meta-research-smoke` 和 `meta-research-formal`，使它们拥有各自的 data root、Codex CLI、`CODEX_HOME` 与 session。前四层使用可抛弃的 smoke 部署，第五层改用全新 formal 部署。这样失败时更容易判断是安装、宿主能力还是研究流程的问题：

1. **安装态 smoke**：记录 commit；运行 `version`、`init`、`launch`；确认 Lumen、Companion 和空研究空间可见。
2. **持久化 smoke**：创建一份 draft，`stop` 后重新 `launch`；确认 draft、Snapshot 和浏览器流程可恢复。
3. **宿主/Provider gate**：运行 `doctor` 和完整 conformance，检查 locked version、登录、电源抑制和 MCP；再在 Quest Creation 用 compute probe 检查真实 GPU。每项都应给出 ready 或明确 typed blocker。
4. **micro 执行/计量 smoke**：仅在 smoke root 中启动一次 `retrain` micro-experiment，观察 execution、RM asset 和 RG Formal Measurement；完成后停止该 daemon，不要把该 root 用于正式研究。
5. **端到端正式研究**：改用全新 formal root，创建 direct Quest，不启动 micro-experiment；持续处理 HumanRequest、等待四个 Stage，观察 Bundle Target/TargetRun 的真实实现、训练与评估，打开 Question Tree/历史/evidence/stdout，最后触发一份 Writing 产物并验证停止/重启恢复。

前四层和第 5 层分别在两个部署根中完成，才能说这台目标机器跑通了当前受控全链路。它仍不等于公网安全、多用户、无治理的任意宿主命令或 Ubuntu/WSL2 双平台发布验收。

## 当前边界

- 目标部署矩阵是 Ubuntu 22.04/24.04 x86_64 和 Windows 11 + WSL2；当前优先在完整 Ubuntu 主机实测，同一候选的双平台最终验收尚未完成。macOS 不属于 v1 目标面。
- Web 目前按单机、单用户、loopback 使用设计，未完成公网多用户部署加固。
- Provider 可用性、模型权限、费用、网络和外部站点访问由测试者自己的账号与环境决定。
- 自动测试已经覆盖 Owner、迁移、持久化、Web 状态和真实 Chrome，但不能代替目标机器上的 Provider conformance 与长期实验观察。
- 真正开始长实验前，应先完成备份、配额和机器电源策略检查。

更完整的产品语义和 Owner 边界见 [PRODUCT.md](./PRODUCT.md)。
