# meta_research_buiding —— 大系统施工根目录（脚手架）

本目录是按 `施工说明书-v2.2` 搭建较大系统级项目的**施工根**。构建按**两级分解**推进：**步（用户给 + 验证方法）→ 检查点（模型切到 superpowers 稳定长度 = 一次提交）**，登记在 `ROADMAP.md`。
每个检查点遵循本目录 `CLAUDE.md` 的硬流程：**检查点内部用 superpowers 构建（含其子代理审核）→ 边界 codex 独立外审（≤2 轮）→ commit → build_log 记录**。所有**决策性制品改动**（代码 / prompt / skill / schema / 接口 等，不限代码）都走此流程。

## 布局
- `CLAUDE.md` —— 系统约束（强约束：评审 / 提交 / 记账纪律）。**动决策性制品前先读。**
- `ROADMAP.md` —— 构建路线图（活文档）：步（用户给 + 验证方法）+ 检查点（模型切，superpowers 稳定长度 = 一次提交）。
- `implement_note.md` —— **施工现场快照**（活文档，只写当下：在哪、下一步、坑）。**每次新 session 开工（含换模型）先读它接上现场**（CLAUDE.md §9）。
- `bin/codex-review.sh` —— 检查点边界评审入口（调 `codexro-review`，见 CLAUDE.md §2.1）。
- `build_log/` —— 每个检查点一条施工记录（`INDEX.md` 索引；记录模板见 CLAUDE.md §6）。
- `wildidea_web-master.zip` —— 参考实现归档（已 gitignore；按需解压成 `wildidea_web-master/` 作参考）。
- 系统代码 —— 后续按施工书逐步落入（如 `INTERFACES.md` / `prompts/` / `schemas/` / `docs/` …）。

## 构建循环（详见 CLAUDE.md §0 / §5）
两级分解：**步（你给 + 验证方法）→ 检查点（模型切到 superpowers 稳定长度 = 一次提交）**，都登记在 `ROADMAP.md`。每个检查点：
```
0. 新 session 开工：先读 implement_note.md 接上现场；干活中随状态刷新它（CLAUDE.md §9）
1. 检查点内部：正常用 superpowers 构建（含其子代理审核 / TDD）；在工作区构建、不落内部提交
2. 本地自验（编译 / 测试 / 运行 / 对 prompt·skill 做实读校验），留命令与输出
3. git add <本检查点相关文件>
4. bin/codex-review.sh "<检查点一句话>"     # 检查点边界 codex 外审，第 1 轮
5. 有 BLOCKER → 改 → 再跑一次（第 2 轮，上限）；仍不过则凭反馈自行改、不再送 codex
6. git commit                              # 落检查点提交（codex 过后）
7. build_log/ 写一条记录 + 更新 INDEX.md + 勾 ROADMAP.md + 刷新 implement_note.md → git commit 该记录（`docs(build_log): …`，见 CLAUDE.md §4）
```

## 前置（一次性）
- 评审引擎 = 无写权限用户 `codexro`（已配好；只能读、改不动仓库，见 CLAUDE.md §2.1）。
- **需用户**在“启动构建会话的那个目录”的 `.claude/settings.local.json` 里加放行规则
  `Bash(codexro-review:*)`，否则非交互跑评审会被 auto-mode 分类器拦（agent 不能自加此规则）。
