# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。

- 更新：2026-07-07 08:08 ｜ 位置：治理·脚手架（无 ROADMAP 步；检查点 = 断点续作机制）
- 检查点状态：已过外审（第 1、2 轮均 APPROVE）→ 落检查点提交 + 记账中

## 正在做什么
「跨 session / 跨模型断点续作」机制已建成待入库：本文件 + CLAUDE.md §9 及各节接线；附带修了 §3 模型名硬编码、0003 遗留 NIT 措辞、评审脚本排除记账类。外审两轮 APPROVE；第 2 轮余留 2 SHOULD + 1 NIT 已按 §2.2 自行落实（送审范围口径统一、模板"临时约定"改"临时执行指针"、本文件状态刷新）。

## 工作区状态
- 全部改动已 staged：CLAUDE.md、README.md、ROADMAP.md、bin/codex-review.sh、implement_note.md（本文件首次引入）
- 评审产物：/tmp/review_out_0004_r1.md（APPROVE，2 SHOULD+1 NIT）、/tmp/review_out_0004_r2.md（APPROVE，2 SHOULD+1 NIT）

## 下一步动作（按序，具体到命令/文件）
1. `git commit` 落检查点提交（docs(rules): 断点续作机制 implement_note.md）
2. 写 `build_log/0004-implement-note-handoff.md`（§6 模板，引用检查点 hash）+ `build_log/INDEX.md` 追加行
3. 刷新本文件为「空闲」→ `git add build_log/ implement_note.md && git commit -m "docs(build_log): 0004 断点续作机制 记录"`
4. 空闲后：等用户给步①及其验证方法（登记进 ROADMAP.md）

## 关键上下文 / 坑（新 session 不读会踩的）
- 评审放行前置未落地：项目 `.claude/settings.local.json` 不存在，无 `Bash(codexro-review:*)` / `Bash(bin/codex-review.sh:*)` 放行规则（0001/0003 遗留，需用户做）；非交互跑评审可能被拦。
- codexro 评审报 401 refresh_token_reused / token_expired 时：`cp /root/.codex-openai-account/auth.json /home/codexro/.codex/auth.json && chown codexro:codexro /home/codexro/.codex/auth.json && chmod 600 /home/codexro/.codex/auth.json`（根因：主/副本共享会轮换的 refresh_token，谁后跑谁有效）。
