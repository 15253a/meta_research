# 2026-09-24 仓库整理记录

保留 main、baseline/v0.0.0、v1-test。运行服务未重启，研究数据未迁移。

## 分支

旧分支在完整 Git bundle 备份后，通过同一次原子 push 建立归档标签并删除分支引用；逐分支使用原 SHA 的 lease，避免覆盖并发更新。

- `codex/bundle-target-root-lifecycle` → `archive/2026-09-24/codex/bundle-target-root-lifecycle` (`81dd67831838bc53b4540506ec454f38affd72f3`)
- `codex/issue-104-reasoning-stage` → `archive/2026-09-24/codex/issue-104-reasoning-stage` (`f2d3f3f0d77a6f50ab535d50d6d404a525c09757`)
- `codex/issue-107-ui-prototype` → `archive/2026-09-24/codex/issue-107-ui-prototype` (`d7e2c9b79792d82285881f39c3fb2de2dd260d5f`)
- `codex/issue-117-deepfetch` → `archive/2026-09-24/codex/issue-117-deepfetch` (`77235790387d6e297a84cdb60345a1b0b25a9ebf`)
- `codex/issue-76-writing-ui-prototype` → `archive/2026-09-24/codex/issue-76-writing-ui-prototype` (`791d315c9854395437300ffa81d4714bd04be279`)
- `codex/quest-flow-simplification` → `archive/2026-09-24/codex/quest-flow-simplification` (`fe5ce639555380fe1c55193ed450303bb7f6fe41`)
- `develop` → `archive/2026-09-24/develop` (`e308f77a1284ec1e58f29bb68e34a7f3b9ddfb0d`)
- `develop_main` → `archive/2026-09-24/develop_main` (`4c033ac6b04f466648b26258a197d96d0f343b8e`)
- `test-all` → `archive/2026-09-24/test-all` (`7729571c4438e6ff085943703f8e0afb092e8f99`)

## 旧开放 issue

以下八项按用户明确指令永久删除。备份含原 issue 元数据、正文、全部评论、事件、timeline、原生子票及依赖关系；本地与远端备份 SHA-256 一致。删除不表示历史验收通过；已关闭的历史 issue 保留。

- Spec：Meta-research vNext — 面向长期自主、人机协同的科研共生平台（完整系统规格）（原编号 112）
- 实现：Meta-research vNext 15 — A「光谱台」完整生产 Web（原编号 127）
- 实现：Meta-research vNext 18 — Loopback Web 安全边界（原编号 130）
- 实现：Meta-research vNext 19 — 跨平台安装、数据保护与版本迁移（原编号 131）
- 实现：Meta-research vNext 20 — System Steward 严格只读诊断（原编号 132）
- 验收：Meta-research vNext 21 — 多人多电脑真实题目 HITL（原编号 133）
- Spec：Meta-research vNext 22 — 当前 Cycle 可见性、完整 Agent 能力与软约束运行（原编号 136）
- 实现：Meta-research vNext 22.11 — 收敛五类 HumanRequest 的自然语言协作与故障重试（原编号 147）

新地图：[记忆系统设计地图：入库、存储、索引与使用（8768 → v1）](https://github.com/15253a/meta_research/issues/148)。六张决策票、九条原生阻塞关系均已回读核验。

## 备份校验

- 完整 Git bundle：`5f36bfce68130163bed27e2cf19194708dbcd886f2becfcf0c96935404e0fcf3`。
- 八张旧开放票 ZIP：`6f3a101d8109c1af228a2524d014d8271c27d0faebfcf1537ec6361297039d84`。

备份不含新增凭据；历史 Git 内容和旧 issue 为原仓库已有内容。完整备份保存于本地工作区 backups 与远端独立设计工作目录，不作为研究数据备份。
