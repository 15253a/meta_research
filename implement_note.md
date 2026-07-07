# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-07 09:06 ｜ 位置：空闲（脚手架 + 治理规则完成至 build_log/0005；ROADMAP 尚无步）
- 检查点状态：空闲

## 正在做什么
无进行中检查点。治理规则已含：断点续作机制（0004）、位置双写权威归属 + 工程质量基线 §10（0005，尺子条经用户手工细化：零上下文试读用便宜模型如 Sonnet / codex 执行）。等待用户给**步①及其验证方法**。

## 工作区状态
- 干净（无未提交改动）。

## 下一步动作（按序，具体到命令/文件）
1. 用户给出步① + 验证方法后：登记进 `ROADMAP.md`（§5 动作 A），切出首个检查点（动作 B）
2. 首个检查点开工：按 CLAUDE.md §5 循环走（本文件登记开工状态）；构建一律执行 §10 质量基线

## 关键上下文 / 坑（新 session 不读会踩的）
- 用户可能在 session 间隙手动清理 / 调整工作区文件（2026-07-07 08:20 曾手动清空 build_log/，已从 git 恢复，见 0005 遗留）。遇 `git status` 莫名 `D` 记录：先与用户确认是否有意，无意则 `git restore --staged --worktree <path>`；勿把意外删除提交进检查点。
- 评审放行前置未落地：项目 `.claude/settings.local.json` 不存在，无 `Bash(codexro-review:*)` / `Bash(bin/codex-review.sh:*)` 放行规则（需用户做）。
- codexro 评审报 401 refresh_token_reused / token_expired 时：`cp /root/.codex-openai-account/auth.json /home/codexro/.codex/auth.json && chown codexro:codexro /home/codexro/.codex/auth.json && chmod 600 /home/codexro/.codex/auth.json`。
- 被建系统素材在父目录 `meta-research/`（施工说明书-v2.2.md 及 _core、INTERFACES.md、prompts/）——启动步①前先读。
