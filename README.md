# meta_research_buiding —— 大系统施工根目录（脚手架）

本目录是按 `施工说明书-v2.2` 搭建较大系统级项目的**施工根**。所有**决策性制品改动**（代码 / prompt / skill / 系统提示 / schema / 接口 等，不限代码）遵循本目录
`CLAUDE.md` 的硬流程：**决策性改动 → codex 评审（≤2 轮）→ commit → build_log 记录**。

## 布局
- `CLAUDE.md` —— 系统约束（强约束：评审 / 提交 / 记账纪律）。**动决策性制品前先读。**
- `bin/codex-review.sh` —— pre-commit 评审入口（调 `codexro-review`，见 CLAUDE.md §2.1）。
- `build_log/` —— 每次提交的施工记录（`INDEX.md` 索引；记录模板见 CLAUDE.md §6）。
- `wildidea_web-master.zip` —— 参考实现归档（已 gitignore；按需解压成 `wildidea_web-master/` 作参考）。
- 系统代码 —— 后续按施工书逐步落入（如 `INTERFACES.md` / `prompts/` / `schemas/` / `docs/` …）。

## 一次决策性改动的循环（详见 CLAUDE.md §5）
```
1. 改动制品（代码 / prompt / skill / 系统提示 / schema / 接口 …）
2. 本地自验（编译 / 测试 / 运行 / 对 prompt·skill 做实读校验），留命令与输出
3. git add <本次相关文件>
4. bin/codex-review.sh "<一句话决策>"     # 第 1 轮评审
5. 有 BLOCKER → 改 → 再跑一次（第 2 轮，上限）；仍不过则凭反馈自行改、不再送 codex
6. git commit                              # 是否提交 / 粒度自行判断，勿过度提交
7. build_log/ 写本次记录 + 更新 INDEX.md → git commit 该记录（`docs(build_log): …`，见 CLAUDE.md §4）
```

## 前置（一次性）
- 评审引擎 = 无写权限用户 `codexro`（已配好；只能读、改不动仓库，见 CLAUDE.md §2.1）。
- **需用户**在“启动构建会话的那个目录”的 `.claude/settings.local.json` 里加放行规则
  `Bash(codexro-review:*)`，否则非交互跑评审会被 auto-mode 分类器拦（agent 不能自加此规则）。
