# 0005 · 位置双写权威归属（保留冗余兜底）+ 工程质量基线 §10

- date: 2026-07-07
- commit: ccfcdb8 — docs(rules): 位置双写权威归属（保留冗余兜底）+ 工程质量基线 §10
- branch: main
- 检查点 / 步: —（治理变更，非 ROADMAP 内的某一步）

## 决策
两条规则均来自用户明确指示：

1. **位置指针双写的权威归属**。0004 引入 implement_note.md 后，其「位置」字段与 ROADMAP.md「当前位置」构成双写。向用户提出 A（删 ROADMAP 冗余行）/ B（保留双写 + 定权威归属）两案，**用户选 B**——理由：implement_note.md 是覆盖式更新、有误写/丢失风险；ROADMAP 位置行粗粒度、入 git，可兜底恢复。落地：CLAUDE.md §9 新增「位置指针双写（故意冗余）」条（implement_note.md 为权威、ROADMAP 只在记账时同步、不一致以 implement_note.md 为准）；§5 步 9 加"并同步其「当前位置」"；ROADMAP「当前位置」节加同步时点注记；README 步 7 同步措辞。
2. **工程质量基线**。用户指示：系统构建的代码一定要具有可维护性与可读性。落地：CLAUDE.md 新增 §10（硬要求：为零上下文接手者写、命名/单一职责/边界、注释只说"为什么"、契约变更同步文档、"新 session 短时间说清模块"的验收尺子）；并接入评审闸门——§2.1 prompt 模板与 bin/codex-review.sh 审查重点行加"可维护性/可读性（§10）"，难读难维护 [SHOULD] 起报、结构性失控可上 [BLOCKER]。

## 改动文件
- `CLAUDE.md` — 修改：§9 加双写权威条；§5 步 9 同步措辞；§2.1 模板审查项；新增 §10
- `ROADMAP.md` — 修改：「当前位置」节加冗余兜底注记（只在记账时同步、实时以 implement_note.md 为准）
- `README.md` — 修改：循环步 7 加「含同步当前位置」
- `bin/codex-review.sh` — 修改：审查重点行加可维护性/可读性（仅 echo 文案）
- `implement_note.md` — 记账类常规刷新（开工登记→过审→空闲），不进外审 diff

## Review（codexro-review，gpt-5.5/xhigh，ephemeral）
- **第 1 轮：APPROVE**（无 BLOCKER；1 SHOULD + 1 NIT）：
  - [SHOULD] §9 新条款写"本文件的「位置」字段"，在 CLAUDE.md 语境易被读成 CLAUDE.md 自身 → 采纳 reviewer 原话：改为明写 `implement_note.md`（3 处指代全部显式化）；
  - [NIT] README 步 7 未带出"同步当前位置" → 采纳补齐。
- 未起第 2 轮：两条均为 reviewer 原话建议、零语义变化的指代/同步措辞，APPROVE 后直接采纳（同 0002 先例，§2.2 上限内）。
- 未采纳意见：无。评审产物：`/tmp/review_out_0005_r1.md`。评审 prompt 中已声明"保留冗余是用户拍板，勿建议去重"，reviewer 未质疑方案本身。

## 验证
- 命令与关键输出：
  ```
  $ grep -n '§10' CLAUDE.md bin/codex-review.sh → §2.1:108 / 脚本:36 / 章节 335 存在
  $ grep -c '位置指针双写' CLAUDE.md → 1；grep -c '只在每个检查点记账时同步' ROADMAP.md → 1
  $ fence 配对：CLAUDE.md=20 README.md=2 ROADMAP.md=0 implement_note.md=0（均偶）
  $ bash -n bin/codex-review.sh → SYNTAX-OK
  $ codexro-review 第1轮 → VERDICT: APPROVE
  ```
- 步级验证：不适用（治理变更；ROADMAP 尚无步）。
- 结论：**通过**。

## 遗留 / 回退
- **⚠️ 本检查点构建期间 `build_log/` 工作区文件被清空**：08:20 左右目录被清空（本 session 未执行任何删除；**事后用户确认：系用户手动清理，非环境故障**）。已用 `git restore --staged --worktree build_log/` 从 HEAD 完整恢复（INDEX.md md5 与 HEAD 一致）。教训仍成立：**git 之外的工作区文件（含未入库的 implement_note.md）不受保护**——这正是本次用户选 B（ROADMAP 兜底）的现实佐证。若再遇 `git status` 出现莫名 `D` 记录：先与用户确认是否有意清理，无意则 `git restore --staged --worktree <path>` 恢复；勿把意外删除提交进检查点。
- 待办（承 0001/0003/0004，需用户）：`.claude/settings.local.json` 放行规则仍未落地。
- 回退：`git revert ccfcdb8`（及本日志提交）。
