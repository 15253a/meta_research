# 0004 · 断点续作机制 implement_note.md（跨 session / 跨模型接力）

- date: 2026-07-07
- commit: bac84ef — docs(rules): 断点续作机制 implement_note.md（跨 session/跨模型接力）
- branch: main
- 检查点 / 步: —（治理变更：给构建流程加"现场快照/接力"能力，非 ROADMAP 内的某一步）

## 决策
用户要求：实现过程中维护一个 `implement_note.md`，记录当前实现进度，让下一次（可能换模型的）session 无缝接着执行。落地为：

- **CLAUDE.md 新增 §9「断点续作」**：`implement_note.md` = 施工现场快照（活文档、覆盖式、只写当下、一屏内），与 `ROADMAP.md`（计划骨架）、`build_log/`（已完成台账）三分工不重叠。规定：
  - 更新时点：检查点开工 / 现场状态每变一档 / 记账时 / **session 可能中断前**（接力生命线）；
  - 性质 = **记账类文件**：常规刷新不算决策性改动、不进外审 diff、随记账提交入库；**豁免硬边界**：常规刷新只写状态/进度/下一步指针，决策性内容必须落受审制品、本文件不能成为其唯一载体（防绕过外审）；
  - 入库时点：检查点提交可含当时快照，记账提交再刷新为空闲——两个快照各以其时点为真；
  - 现场真相 = 工作区最新版（未提交也算数）；新 session 开工序：CLAUDE.md → implement_note.md → ROADMAP.md → build_log/。
- **各节接线**：preamble 开工必读；§0 ②④；§1"不算"清单；§2.1 diff 排除示例；§4 记账命令；§5 新增步 0 并改步 1/4/9；§7 新增红线；铁律2/§2/§8 送审范围口径统一为"staged diff（记账类除外）"。
- **附带清理（同属"跨模型无缝"动机）**：§3 Co-Authored-By 原硬编码 "Claude Opus 4.8 (1M context)" 已与当前 harness（Fable 5）冲突 → 改为以当前会话 harness 署名为准、勿硬编码；统一 0003 遗留 NIT 措辞（"一句话决策/本次决策"→"检查点一句话"、脚本 :30 提示）。

影响面：仅施工约束/流程文件，无运行期系统代码。

## 改动文件
- `CLAUDE.md` — 修改：新增 §9 + 上述各节接线 + 口径统一 + §3 去署名硬编码 + NIT 措辞统一
- `README.md` — 修改：布局加 implement_note.md；构建循环加步 0（开工先读）、步 7 加刷新
- `ROADMAP.md` — 修改：头部循环说明加刷新 implement_note.md + 现场快照指引一句
- `bin/codex-review.sh` — 修改：外审 diff 排除清单加 `:(exclude)implement_note.md`；提示措辞"本次决策"→"本检查点"、排除说明改"记账类"
- `implement_note.md` — 新增：仓库根施工现场快照（初始版，首次引入随本检查点受审；此后常规刷新按 §9 豁免）

## Review（codexro-review，gpt-5.5/xhigh，每轮全新 ephemeral）
- **第 1 轮：APPROVE**（无 BLOCKER；2 SHOULD + 1 NIT）：
  - [SHOULD] 记账类豁免边界不够硬（模板"取舍、临时约定"可诱导把决策伪装成现场上下文）→ 采纳：§9 写死"常规刷新 = 只写状态/进度/下一步指针；决策性内容必须落受审制品、本文件不能成为唯一载体"；
  - [SHOULD] implement_note.md 入库时点有字面歧义 → 采纳 reviewer 给的明确化：§9 新增"入库时点"条；
  - [NIT] §3 示例仍写 `Claude <当前模型名>` → 采纳：删示例，纯中性表述。
  - 虽第 1 轮已 APPROVE，因新增了两句规范性措辞，仍将修订后全量 diff + 逐条回应送第 2 轮复核（未超 ≤2 轮上限）。
- **第 2 轮：APPROVE**（无 BLOCKER；余 2 SHOULD + 1 NIT，按 §2.2 于 APPROVE 后自行落实、不再送审）：
  - [SHOULD] 铁律2/§2/§8 仍写"全量 staged diff"与排除规则不一致 → 采纳（reviewer 建议措辞）：统一为"staged diff（记账类除外，§9）"，§0② 同步；
  - [SHOULD] §9 模板仍留"临时约定" → 采纳（reviewer 建议措辞）："临时执行指针（涉及规则/验收/取舍时，结论本体见受审制品）"；
  - [NIT] implement_note.md 状态字段停在"待外审第 1 轮" → 属 §9 记账类刷新，提交前已刷新。
- 未采纳意见：无。评审产物：`/tmp/review_out_0004_r1.md`、`/tmp/review_out_0004_r2.md`（含 codex 自行 `bash -n`、`git diff --staged --check` 复核通过的记录）。

## 验证
- 命令与关键输出：
  ```
  $ grep -rn 'Opus 4.8|一句话决策|本次决策' （排除 build_log 历史） → 仅 implement_note.md 叙述"修了什么"一处，制品无残留
  $ grep -c '§9' CLAUDE.md README.md ROADMAP.md → 8 / 2 / 1；'^## 9\.' 存在（§0–§9 章节完整）
  $ grep -n '全量 staged diff' CLAUDE.md → (无残留，四处已统一"记账类除外"口径)
  $ fence 配对：CLAUDE.md=20 README.md=2 ROADMAP.md=0 implement_note.md=0（均偶数）
  $ bash -n bin/codex-review.sh → SYNTAX-OK
  $ codexro-review 第1轮/第2轮 → 均 VERDICT: APPROVE
  ```
- 步级验证：不适用（治理变更，非 ROADMAP 步；ROADMAP 尚无步）。
- 结论：**通过**（约束文件自洽、脚本语法通过、评审闭环两轮走通；本机制自身即以本检查点为首个实例运转：现场快照已随流程三次刷新）。

## 遗留 / 回退
- **待办（需用户，承 0001/0003）**：项目 `.claude/settings.local.json` 仍不存在，`Bash(codexro-review:*)`、`Bash(bin/codex-review.sh:*)` 放行规则未落地——本轮评审是在会话内直接授权跑通的，非交互/自治场景仍可能被拦。
- **codexro 认证脆弱性**（承 0003）：单 refresh_token 双消费者问题未根治；修法已写入 implement_note.md「关键上下文」。
- 回退：`git revert bac84ef`（及本日志提交）。机制回退后需同步删除仓库根 `implement_note.md`（revert 会自动删）。
