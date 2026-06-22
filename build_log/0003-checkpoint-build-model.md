# 0003 · 确立两级分解(步→检查点)+检查点闭环构建模型

- date: 2026-06-22
- commit: a45c59b — docs(rules): 确立两级分解(步→检查点)+检查点闭环构建模型
- branch: main
- 检查点 / 步: —（治理变更：建立"构建模型"本身，非 ROADMAP 内的某一步）

## 决策
把"如何拿一个大目标推进构建"的方法学写进 CLAUDE.md，补上原先缺失的**宏观层**。
与用户对齐后定下的模型：

- **两级分解**：步（Level 1，用户给 + 每步验证方法）→ 检查点（Level 2，模型切到
  superpowers 能稳定设计的长度 = 一次对外提交）。两级登记在新增 `ROADMAP.md`（活文档）。
- **检查点闭环**：内部正常用 superpowers 构建（含其自带子代理审核 `requesting-code-review`）
  → 检查点边界开 codex 独立外审（≤2 轮）→ 过后一次性提交 → build_log 记账。
- **审查两层、互不替代**：superpowers 子代理 = 检查点内部把关；codex = 检查点边界独立外审。
  这**修订**了原 §8/§7「停用 requesting-code-review、评审只认 codex」——改为内部审核恢复启用、
  codex 退到边界做闸门。
- codex 触发点从「每次提交前」改判为「**检查点边界**」。

影响面：CLAUDE.md 行为契约（评审触发粒度、审查归口）、README、新增 ROADMAP、评审脚本注释口径。

## 改动文件
- `CLAUDE.md` — 修改：§0 重写为「构建模型 + 三条铁律」；§2 标题/引子改为「检查点边界」；
  §3 提交规范补检查点编号；§4 日志提交命令含 `ROADMAP.md`、"决策"口径→"检查点"；
  §5 动作清单重写为「宏观分解 + 每检查点循环」、步级验证移到写 build_log 之前；
  §6 模板加「检查点/步」「步级验证」字段；§7 红线两条改写；§8 评审归口改为两层并存。
- `README.md` — 修改：顶部总述/布局/循环同步到两级分解 + 检查点闭环；布局加 `ROADMAP.md`。
- `ROADMAP.md` — 新增：活文档骨架（当前位置 + 步/检查点登记模板；尚无步，待用户给）。
- `bin/codex-review.sh` — 修改：注释口径「pre-commit / 一句话决策」→「检查点边界 / 检查点一句话」（仅注释，行为不变）。

## Review（codexro-review，gpt-5.5/xhigh）
> 评审前置事故：codexro 的 `auth.json` 副本（last_refresh 2026-06-08）已被服务器轮换失效，
> 首跑报 `refresh_token_reused / token_expired`。按 §2.1 补救——从主 CODEX_HOME
> `/root/.codex-openai-account/auth.json`（last_refresh 2026-06-19）重拷到 `/home/codexro/.codex/`、
> 修属主/权限（旧副本备份为 `auth.json.stale`）。重跑即通。

- **第 1 轮：REQUEST_CHANGES**（3 BLOCKER + 2 SHOULD + 1 NIT）
  - [BLOCKER] 累计 diff 缺口：文档说"内部可有 WIP 提交 + codex 审累计 diff"，但脚本只审 `git diff --staged`，WIP 一旦提交边界就审不全。
  - [BLOCKER] `ROADMAP.md` 勾选漏提：§4 命令仅 `git add build_log/`。
  - [BLOCKER] 步级验证顺序反：清单先提交 build_log 再跑步级验证，而 build_log 又要求含其结果。
  - [SHOULD] README 顶部总述仍旧契约；[SHOULD] INDEX.md 表头 + §4「每个决策」旧口径；[NIT] 脚本注释旧口径。
- **修正（逐条采纳，均为真问题）**
  - 改模型为「**检查点内部不落 git 提交**（工作区构建）→ 边界 `git add` 全量 → codex 审 staged 全量 diff → 一次性提交」，消除累计 diff 缺口、且对上用户"commit 只在 codex 之后"。
  - §4 命令改 `git add build_log/ ROADMAP.md`；§5 步级验证移到写/提交 build_log 之前。
  - README 顶部、INDEX.md 表头、§4 口径、脚本注释一并迁移到"检查点"。
- **第 2 轮：APPROVE**
  - 余 [NIT]：脚本空暂存提示 `codex-review.sh:30`、CLAUDE.md:106/:144 仍有"一句话决策/本次决策"示例措辞。reviewer 明示"语义不破坏新流程，建议后续统一"。
  - **未采纳（本轮）该 NIT**：纯措辞、非破坏；为保证"提交内容 = 受审内容"，未在 APPROVE 后再改。留待后续小检查点统一。

## 验证
- 命令与关键输出：
  ```
  $ bash -n bin/codex-review.sh        → SYNTAX-OK
  $ grep §x 交叉引用                   → §1 §2 §2.1 §2.2 §3 §4 §5 §6 §7 §8 全部有对应章节，无悬空
  $ 残留旧措辞清查
      停用 / pre-commit reviewer / 每次决定要 commit / 替代它 / 正交各管各 → (无)
      WIP 允许性表述 / 累计 diff                                        → (无)
  $ fence 配对                         → CLAUDE.md=16(偶) README.md=2(偶) 均平衡
  $ codex 第2轮                        → VERDICT: APPROVE
  ```
- 结论：**通过**（文档自洽、契约与脚本一致、评审闭环端到端走通，含一次真实的认证故障恢复）。
- 性质说明：本次为流程文档/约束改动，无运行期代码；"验证"= 一致性 + 评审，无单测可跑。

## 遗留 / 回退
- **NIT 待办**：`bin/codex-review.sh:30`、`CLAUDE.md:106/:144` 的"一句话决策/本次决策"措辞，后续小检查点统一为"检查点"。
- **codexro 认证脆弱性**：主与 codexro 共享一份会轮换的 refresh_token，谁后跑谁有效、另一方失效（本次即主 Jun-19 比副本 Jun-08 新所致）。补救已知（从主重拷），但根因是单凭证双消费者；若主也失效需用户交互 `codex login`。备份 `/home/codexro/.codex/auth.json.stale` 可手动清理。
- **放行规则**（承 0001）：需用户在启动会话目录 `.claude/settings.local.json` 保留 `Bash(bin/codex-review.sh:*)`、`Bash(codexro-review:*)`。
- 回退：`git revert a45c59b`（及本日志提交）。
