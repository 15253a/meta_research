# 0001 · 施工脚手架 baseline

- date: 2026-06-17
- commit: f968be9 — chore: 施工脚手架 baseline
- branch: main

## 决策
为按施工说明书搭建较大系统级项目，先在本目录建立"施工脚手架"：git 仓库 + pre-commit 评审入口 + 施工说明 + 日志索引。后续系统代码在此仓库内按 CLAUDE.md 硬流程逐步落入。

## 改动文件
- `.gitignore` — 新增：忽略 *.zip（参考实现归档）等杂物
- `CLAUDE.md` — 新增：系统约束（评审 / 提交 / 记账硬流程）；本轮另修订 §4/§5：明确 build_log 记录须作为紧随的小提交入库、勿 amend（修 reviewer 指出的 BLOCKER）
- `bin/codex-review.sh` — 新增：评审入口，`git diff --staged` 内联 + 调 `codexro-review`（模式 A：codex 以无写权限 codexro 读仓库）
- `README.md` — 新增：施工根说明与决策循环
- `build_log/INDEX.md` — 索引追加 0001

## Review（codexro-review，gpt-5.5；本次 low effort 验证脚手架）
- 第 1 轮：**REQUEST_CHANGES** —— [BLOCKER] build_log 不入库（审计记录脱离 git、流程断裂）；[SHOULD] `/tmp/codexrev-$ts` 可预测+并发覆盖；[SHOULD] `git diff … || true` 吞错误。
- 修复：CLAUDE.md §4/§5 明确日志作为第二个提交入库且勿 amend；脚本改 `mktemp -d` + 去掉 `|| true`；另给 codexro 配 git `safe.directory=*`。
- 第 2 轮：**APPROVE** —— 余 [SHOULD] 777 目录可被他人篡改、[NIT] README 漏写"提交日志"、[NIT] 保留策略未注明。
- 采纳（2 轮上限内，未再起第 3 轮）：临时目录 `chmod 777` → `chown codexro`(700)；README 循环补"提交 build_log"；脚本注明 /tmp 目录故意保留。
- **未采纳**：[NIT] 目录名拼写 `meta_research_buiding` —— 刻意保留，与既定施工书/项目计划（auto-research-metaloop）一致。

## 验证
- `bash -n bin/codex-review.sh` → `SYNTAX-OK`
- `bin/codex-review.sh "<决策>" -c model_reasoning_effort=low`（第 1、2 轮）：codex 以 `sandbox: danger-full-access`、`workdir=<repo>`、身份 codexro 跑通，产出分级评审 + VERDICT；第 2 轮 `VERDICT: APPROVE`。
- 结论：**通过**（脚手架 + 评审闭环端到端可用；codex 能读仓库、改不动）。

## 遗留 / 回退
- 待办（**需用户**）：在"启动构建会话的目录"的 `.claude/settings.local.json` 加放行规则 `Bash(bin/codex-review.sh:*)`、`Bash(./bin/codex-review.sh:*)`、`Bash(codexro-review:*)`，否则非交互跑评审会被 auto-mode 分类器拦（agent 不能自加）。
- 注意：codexro 的 `auth.json` 是副本，失效则从 root 的 CODEX_HOME 重拷。
- 回退：`git revert f968be9`（及本日志提交）。
