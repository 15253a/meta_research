# meta_research

高度人机融合的科研管理平台。

## Sandcastle 自动实现队列

本分支使用 Sandcastle 0.12.0 + 当前宿主 Codex 处理 #113–#132。controller
读取 GitHub 原生 `blockedBy`，每轮自动选择最多 3 张 frontier 票据，为每张票
创建独立 branch/worktree，并行调用 Matt Pocock `$implement`。一轮实现全部结束
后，controller 会逐张做集成验证、push、创建并自动合并以 `develop_main` 为 base
的 PR，随后复验、关闭票据并重新查询下一轮 frontier。

### 初始化

不需要 Docker、镜像构建或单独登录。直接复用当前 shell 中已经登录的 Codex：

```bash
cd /vepfs-mlp2/c20250511/250806010/mxm/paper_agent/meta_research_main
npm ci
cp .sandcastle/.env.example .sandcastle/.env
chmod 600 .sandcastle/.env
npm run sandcastle:preflight
```

`sandcastle:preflight` 会检查本机 `codex`、当前登录、当前 `$CODEX_HOME`，并通过
Codex app-server 的 `skills/list` 确认 Matt Pocock 稳定包的 25 个 skills 都可见。
其中包括 `$implement` 的直接依赖 `$tdd`、`$code-review`，以及它们引用的其他
skills、模板和说明文件。配置、登录、skills、插件等均直接使用当前本机 Codex，
不会再复制或镜像一份。

如需做一次真实但只读的模型工作流验证：

```bash
npm run sandcastle:smoke-implement
```

### 启动

先只读确认下一张票，再启动持续队列：

```bash
npm run sandcastle:auto -- --dry-run
npm run sandcastle:auto
```

默认并行度是 3，也可以临时降低（不能超过 3）：

```bash
npm run sandcastle:auto -- --max-agents 1
```

这里的并行度指顶层 ticket Agent。`$implement` 内部使用的 `$tdd`、
`$code-review` 等工作流不占额外 Sandcastle ticket slot。GitHub 当前如果只有一张
unblocked frontier（例如首轮只有 #113），controller 只会启动 1 个 Agent；只有
依赖图同时放出 2–3 张票时才会并行。

每张票的普通 Git worktree 都位于：

```text
meta_research_main/.sandcastle/worktrees/
```

其他由你管理、位于该目录之外的 Git worktree 可以照常存在；controller 只会因
上次失败而遗留的 Sandcastle managed worktree 停住并要求恢复。

Sandcastle 使用本机 Codex 直接在该 worktree 中实现。`$implement` 的当前
`SKILL.md` 以及 `$tdd`、`$code-review` 会被 controller 确定性展开进非交互
prompt；完整本机 skills 包仍可由 Codex 正常发现和读取。

这是明确的 host/no-Docker 模式：Agent 与 controller 使用同一个宿主账号，能
访问该账号本来就能访问的本机文件、网络和凭据。适合这里的可信仓库与可信
ticket，不再提供容器级隔离。Agent 子进程由独立 wall-clock timeout 兜底，避免
Sandcastle 超时后仍在后台继续修改 worktree；parent-death signal 与独立 runtime
lock 也会在 controller 异常退出时终止或标记仍存活的 Agent，`--retry` 不会抢先
删除它正在使用的 worktree。

自动流程如下：

```text
GitHub native frontier → 选最多3张 → claim
→ 3个独立 worktree 中并行 $implement + verify
→ 等待本轮全部完成
→ 逐张 push/PR → 对最新 develop_main 做 integration verify
→ atomic auto merge → exact-commit host verify → close issue
→ 重新查询下一轮 frontier
```

后一个 sibling 在前一个 PR 合并后会针对最新 `develop_main` 重新生成并验证合并
树，并把 `{verified tree, current base, candidate}` 制成确定的 merge commit，再用
`force-with-lease` 对 `develop_main` 做原子 compare-and-swap。PR head、merge commit
双亲与最终 tree 全部精确匹配后才会接受。因此 Agent 执行可以并行，GitHub 发布
和合并保持确定性的串行顺序。

合并后的复验从精确 accepted commit 导出到
`.sandcastle/inbox/post-merge-*`，而不是验证可移动的 `develop_main` HEAD；完成后
自动清理。候选仍不得修改 `.sandcastle/`、`.agents/`、`.codex/`、`.github/`、
根 controller package 文件、Git 配置/hooks 或其他 refs。

### 查看与恢复

```bash
npm run sandcastle:status
npm run sandcastle:status -- --watch
```

完整日志、结果和 controller 当前阶段分别位于 `.sandcastle/logs/`、
`.sandcastle/receipts/`、`.sandcastle/status.json`；每张并行票据的实时阶段位于
`.sandcastle/status/issue-N.json`。`sandcastle:status` 会同时显示并行上限、占用
slot、当前 cohort 和下一轮 frontier。

若状态给出 `resumeStage`，修复外部条件后运行：

```bash
npm run sandcastle:auto -- --resume --issue 113
```

若实现被中断且状态建议重试：

```bash
npm run sandcastle:auto -- --retry --issue 113
```

只有一个失败 checkpoint 时可以省略 `--issue`；多个并行票据同时需要处理时必须
显式指定。`--retry` 只清理精确匹配且干净的残留 worktree；未提交证据不会被
静默删除。单票默认最长 8 小时，可在 `.sandcastle/.env` 调整为 1–24 小时。

调试单张票仍可使用：

```bash
npm run sandcastle:ticket -- --issue 113 --base-ref develop_main
```
