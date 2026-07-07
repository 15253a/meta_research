# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。

- 更新：2026-07-07 08:08 ｜ 位置：空闲（脚手架已就绪；ROADMAP 尚无步）
- 检查点状态：空闲

## 正在做什么
无进行中检查点。脚手架 + 治理规则已完成到 build_log/0004（断点续作机制），等待用户给**步①及其验证方法**。

## 工作区状态
- 干净（无未提交改动）。

## 下一步动作（按序，具体到命令/文件）
1. 用户给出步① + 验证方法后：登记进 `ROADMAP.md`（§5 动作 A），把步①切出首个检查点（动作 B）
2. 首个检查点开工时：按 CLAUDE.md §5 循环走（本文件登记开工状态）

## 关键上下文 / 坑（新 session 不读会踩的）
- 评审放行前置未落地：项目 `.claude/settings.local.json` 不存在，无 `Bash(codexro-review:*)` / `Bash(bin/codex-review.sh:*)` 放行规则（0001/0003/0004 遗留，需用户做）；非交互跑评审可能被拦。
- codexro 评审报 401 refresh_token_reused / token_expired 时：`cp /root/.codex-openai-account/auth.json /home/codexro/.codex/auth.json && chown codexro:codexro /home/codexro/.codex/auth.json && chmod 600 /home/codexro/.codex/auth.json`（根因：主/副本共享会轮换的 refresh_token，谁后跑谁有效）。
- 被建系统的素材在仓库外的父目录 `meta-research/`（施工说明书-v2.2.md 及 _core 精华版、INTERFACES.md、prompts/、参考实现 zip）——启动步①前先读施工说明书对齐。
