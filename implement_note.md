# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。

- 更新：2026-07-07 08:48 ｜ 位置：治理·脚手架（检查点 = 位置双写权威归属 + 工程质量基线）
- 检查点状态：构建中

## 正在做什么
用户定了两条治理规则，落进约束文件：
① 保留 `ROADMAP.md`「当前位置」与本文件「位置」的双写（本文件覆盖式、有丢失风险，ROADMAP 粗粒度兜底），**权威归本文件**、ROADMAP 只在记账时同步——落到 CLAUDE.md §9 新增权威归属条 + §5 步 9 + ROADMAP 注记；
② 工程质量基线：系统构建的一切制品必须**可维护、可读**——新增 CLAUDE.md §10 + 外审审查重点接线（§2.1 模板、bin/codex-review.sh）。

## 工作区状态
- 构建中：CLAUDE.md、ROADMAP.md、bin/codex-review.sh 即将编辑；本文件此条即开工登记（记账类，不进外审 diff）。

## 下一步动作（按序，具体到命令/文件）
1. 完成上述编辑 → 自验（grep §10 交叉引用 / fence 配对 / bash -n）
2. `git add -A` → `git diff --staged -- . ':(exclude)build_log/**' ':(exclude)implement_note.md'` 导出 → 手写 prompt 调 codexro-review（第 1 轮）
3. 过后落检查点提交 → build_log/0005 + INDEX + 本文件刷新「空闲」→ docs(build_log) 提交

## 关键上下文 / 坑（新 session 不读会踩的）
- 评审放行前置未落地：项目 `.claude/settings.local.json` 不存在，无 `Bash(codexro-review:*)` / `Bash(bin/codex-review.sh:*)` 放行规则（0001/0003/0004 遗留，需用户做）。
- codexro 评审报 401 refresh_token_reused / token_expired 时：`cp /root/.codex-openai-account/auth.json /home/codexro/.codex/auth.json && chown codexro:codexro /home/codexro/.codex/auth.json && chmod 600 /home/codexro/.codex/auth.json`。
- 被建系统素材在父目录 `meta-research/`（施工说明书-v2.2.md 及 _core、INTERFACES.md、prompts/）——启动步①前先读。
