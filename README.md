# meta_research
高度人机融合的科研管理平台

## Sandcastle 单票执行环境

本分支包含一个受控的 Sandcastle 0.12.0 + Codex runner。GitHub 是 ticket
状态与依赖的权威来源；runner 只读取一张显式 implementation ticket，创建
独立 named branch/worktree/Docker，并把候选实现停在 `READY_FOR_HITL`。
它不会领取、push、merge、评论或关闭 ticket。

### 初始化

```bash
npm ci
cp .sandcastle/.env.example .sandcastle/.env
# 编辑 .sandcastle/.env，填入受限且可轮换的 CODEX_API_KEY
npm run sandcastle:build-image
```

镜像固定为 Linux x86_64。构建命令先在宿主侧把 Codex CLI 0.148.0 准备到
被忽略的 `.sandcastle/.image-codex/`，再构建 Docker 镜像；注册表凭据和
代理配置不会因此进入镜像。基础镜像同时固定 tag 与平台 digest。

不要把 `GH_TOKEN`、SSH key、`~/.codex/auth.json`、整个 Codex home 或
Docker socket 挂入容器。Sandcastle 0.12.0 会把 `CODEX_API_KEY` 暴露给
整个 sandbox 生命周期，因此当前环境只适用于可信仓库的受控执行；正式
严格隔离需要后续增加单次调用凭证代理。镜像用户会对齐构建者的 UID/GID；
用 root 构建就会得到 root 容器，真实 ticket 应由专用非特权宿主用户构建
和运行。

### 只读检查

```bash
npm run sandcastle:dry-run -- --issue 113 --base-ref develop_main
```

该命令读取 GitHub 和本地 Git，不创建 sandbox、branch 或 tracker 写入。
当前 runner 显式限定实现票 #113–#132，避免误取 #112 Spec 或 #133 真人验收。

### 实际单票执行

先在 GitHub 将目标 frontier ticket 仅分配给当前账号，再运行：

```bash
npm run sandcastle:ticket -- \
  --issue 113 \
  --base-ref develop_main \
  --model <validated-model-id> \
  --verify '<fixed-project-verification-command>'
```

每次运行最多调用一次 Agent。结果写入被忽略的 `.sandcastle/logs/` 和
`.sandcastle/receipts/`，状态只可能进入 `READY_FOR_HITL` 或
`NEEDS_HUMAN`。Sandcastle 必须可写挂载公共 Git 元数据才能形成 commit，
所以 runner 会前后核对本地 config、hooks 与除当前候选分支外的所有 refs；
Agent 总墙钟限制为 60 分钟，独立验证限制为 30 分钟。0.12.0 的 Docker
provider 只有 CPU 参数，没有内存/PID 参数；更强的资源隔离需要后续自定义
provider。为避免影响其他工作树，真实单票运行时不允许仓库中同时存在其他
linked worktree；保留的失败 attempt 必须先由人处理。即使测试通过，也不会
自动形成领域接纳。
