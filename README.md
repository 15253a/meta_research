# meta_research
高度人机融合的科研管理平台

## Sandcastle 自动实现队列

本分支使用 Sandcastle 0.12.0 + Codex 顺序处理 #113–#132。宿主 controller
读取 GitHub 原生 `blockedBy`，自动选择 frontier、领取当前账号、调用
`$implement-ticket`、验证、push、创建并合并以 `develop_main` 为 base 的 PR，
随后复验、关闭对应 ticket，并继续下一张。正常路径不需要逐票人工操作。

### 初始化

```bash
npm ci
cp .sandcastle/.env.example .sandcastle/.env
chmod 600 .sandcastle/.env
# 只需编辑一次：填入受限且可轮换的 CODEX_API_KEY；模型已有默认值
npm run sandcastle:build-image
```

镜像固定为 Linux x86_64。构建命令先在宿主侧把 Codex CLI 0.148.0 准备到
被忽略的 `.sandcastle/.image-codex/`，再构建 Docker 镜像；注册表凭据和
代理配置不会因此进入镜像。基础镜像同时固定 tag 与平台 digest。

GitHub 与 SSH 凭据只在宿主 controller；Agent 容器只得到 `CODEX_API_KEY`，
没有 `gh`、SSH key、Docker socket 或宿主 Codex home。Sandcastle 0.12.0 会
让该 key 在 sandbox 整个生命周期内可见，因此本环境只运行可信仓库代码。
镜像用户与构建者 UID/GID 对齐；专用非特权宿主用户仍是长期运行的推荐方式。

### 启动自动队列

```bash
npm run sandcastle:auto -- --dry-run
npm run sandcastle:auto
```

第二条命令会持续运行。任何 GitHub 写入前，它先检查 API key、所选模型与
本地镜像；固定验证通过后直接创建并同步合并 PR，再自动复验、关票并重新查询
frontier。宿主/API 的短暂失败会自动退避重试；本地 checkpoint 与每票唯一的
远端 lease 防止重启或另一台同账号机器重复执行。在发布/合并阶段按 `Ctrl+C`
停止后，运行同一命令会与远端 PR 对账恢复。若在 Agent 执行中中断，已有
branch/worktree 会被保留并显示 `NEEDS_HUMAN`，避免静默丢弃工作；实现、
验证、合并冲突或票据语义变化才会停住当前票，不会跳过它。

若状态中给出 `resumeStage`，修复所报告的外部条件后运行
`npm run sandcastle:auto -- --resume`；若实现过程被中断且状态建议重试，运行
`npm run sandcastle:auto -- --retry`。后者只会移除精确匹配且干净的残留
worktree，保留旧 branch 的 commits，并为同一票创建全新 attempt；若仍有未提交
证据，它会要求先提交或另行归档。没有对应状态时，这两个参数都会拒绝运行。
Agent 单票默认最长 8 小时，可在 `.sandcastle/.env` 中调整为
1–24 小时；连续基础设施错误重试 8 次后会停在可恢复状态，不会永久空转。

实时查看：

```bash
npm run sandcastle:status
npm run sandcastle:status -- --watch
```

终端同时显示 Agent 文本与工具调用；完整日志和结果分别位于
`.sandcastle/logs/`、`.sandcastle/receipts/`，当前阶段位于
`.sandcastle/status.json`。

### 自动流程

```text
native frontier → auto claim → $implement-ticket → verify → push/PR
→ auto merge → post-merge verify → close issue → next frontier
```

固定验证入口是 `.sandcastle/verify-ticket.sh`。它要求产品保留锁定的 Python
发行物、执行测试/构建，并调用仓库公共入口 `scripts/verify`；因此无需每张票
重新输入 `--verify`。合并后的复验从精确 accepted commit 导出干净副本，在
没有宿主 HOME、GH/SSH/Codex 凭据或 Docker socket 的一次性容器中运行。根
`package.json`、lockfile 与 `tsconfig.json` 专属于 controller，产品前端包放在
独立目录；`.github/` 也不允许候选修改，避免 PR 分支在合并前启动新 workflow。
默认模型是当前本机已验证的 `gpt-5.4`，可在
`.sandcastle/.env` 中一次性覆盖。

需要调试某张票时仍可显式运行底层单票入口：

```bash
npm run sandcastle:ticket -- \
  --issue 113 \
  --base-ref develop_main
```

自动队列当前保持 concurrency 1；它已经消除了逐票选取、领取、传模型与验证
命令、push、建 PR、关票和启动下一票的人工操作。后续若启用并发，需要先把
Git ref/worktree 审计扩展为 batch allow-list，而不能直接套用官方并行模板。
