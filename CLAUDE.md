# 构建大型系统 · 系统约束（CLAUDE.md）

> 本文件是**强约束（hard constraints）**，不是建议。在本目录及其子目录下进行任何系统构建工作时，
> 下列规则 **OVERRIDE 默认行为**，必须逐条照做。违反任何一条 = 本次工作未完成。

适用范围：本目录（`meta_research_buiding/`）所看护的大型系统的全部**决策性制品改动**——不限代码，含 prompt / skill / 系统提示 / schema / 接口定义 / 配置（见 §1）。
施工日志统一落在本目录下：`meta_research_buiding/build_log/`。
**每次新 session 开工（含换模型接手）：先读仓库根 `implement_note.md`（施工现场快照，§9），再动手。**

---

## 0. 构建模型 + 三条铁律（先读本节，其余是展开）

### 构建模型：两级分解 → 检查点闭环

大目标分两级落地：

- **步（Level 1，用户给）**：粗阶段，例如"①流程层 ②资产层"，**每步附一个验证方法**。它比 superpowers 一次能稳的长度大，是不动的主线骨架。用户没给时，按需用 `brainstorming` 先对齐再拆。
- **检查点（Level 2，模型切）**：把当前这一步切成若干**检查点**，每个 = superpowers 能稳定设计/构建的一截 = **一次对外 git 提交**的粒度。怎么切、切多大由模型自行判断（尺子：可独立解释 + 可独立回退 + superpowers 不易跑偏的长度）。后面步依赖前面步的产物，故**做到哪步才切那步的检查点**，不必一次列全。

两级都登记在仓库根的 **`ROADMAP.md`**（活文档）：步 + 每步验证方法 = 不动主线；检查点 = 随进度展开的施工计划 + 完成勾选。

每个检查点跑一个闭环：

- **① 检查点内部**：正常用 superpowers 构建（`brainstorming` 需要时 / `writing-plans` / `executing-plans` / TDD，**含其自带的子代理审核 `requesting-code-review`**）—— 这是"内部把关"，过程可长，但**在工作区内构建、不落内部 git 提交**（子代理审核读工作区即可，无需提交）。
- **② 检查点边界**：把本检查点全部改动 `git add` 后，独立开 codex 对**这份 staged diff（记账类除外，§9）**做外审（≤2 轮，§2）—— "外部独立审计"。因内部不落提交，staged diff 即本检查点全貌（记账类文件 `build_log/`、`implement_note.md` 不进外审 diff，§9）。
- **③ 提交**：codex 过了（或 2 轮上限）**才**落"检查点提交"。
- **④ 记账**：写**一条** build_log（中文给人读：做了什么 / 改了哪些文件 / 做了哪些验证 / 结论）+ 勾 ROADMAP + 刷新 `implement_note.md`（§9）。

> 两层审查各司其职、互不替代：**superpowers 子代理 = 检查点内部把关；codex = 检查点边界的独立外审。** 详见 §8。

### 三条铁律

1. **以检查点为对外提交单元，及时 commit——检查点切多大、是否提交、粒度自行判断。** 检查点 = 一个可独立解释、可独立回退的决策 = superpowers 能稳定拿下的一截。不要把无关决策攒成大杂烩提交；也**不要过度提交**（检查点内部的琐碎微调靠 superpowers 内部流程消化，不必每步都单独成对外 commit）。
2. **每个检查点边界（落该检查点提交之前）→ 必须先用本机 codex 做独立外审，最多反复 2 轮。** 外审未过且未到 2 轮上限，**不许落该检查点提交**。（检查点内部在工作区构建、不落 git 提交；到边界 `git add` 全部后由 codex 审 staged diff（记账类除外，§9），过了再一次性提交。）
3. **每个检查点提交之后 → 必须在 `build_log/` 写一条记录**：这个检查点做了什么、改了哪些文件、是否验证通过。

任意一条没做，这个检查点就**不算完成**，不要向用户声称"已完成 / 已提交 / 已验证"。

---

## 1. 什么算"决策性改动"（必须走完整流程）

**范围 = 任何改变系统行为或对外契约的「制品」改动，不限于代码。** 走完整流程（review → commit → 记录）的包括但不限于：

- **prompt / 系统提示**（如 `prompts/system_prompt.md`）、**skill**（`SKILL.md` 及其引用）—— *它们就是行为本身*；
- **JSON Schema / 数据契约**（如 `schemas/` 四阶段契约）、**接口定义**（如 `INTERFACES.md`）；
- 代码：新增 / 删除 / 重命名模块、接口、数据结构、配置项；改变控制流、算法、并发模型、错误处理策略；
- 改变对外契约（API、CLI、文件格式、DB schema）；引入 / 升级 / 移除依赖；
- **配置**，以及任何会影响别处行为、或难以回退的改动。

> ⚠️ 对 **prompt / skill / 系统提示**：**措辞即行为**，连"小改一句话"通常也是决策性的 —— 按决策处理、走评审，别当排版放过。

**不算**决策性改动、可不单独走流程的：纯排版、**代码注释**错别字、本地实验性涂鸦（未进主干、随手丢弃），以及**记账类文件的常规更新**（`build_log/`、`ROADMAP.md` 勾选、`implement_note.md` 刷新——仅限状态 / 进度 / 下一步指针，边界见 §9；不走外审，随记账提交入库即可）。
拿不准某改动算不算“决策性”时，倾向当决策处理；但**是否单独成一次提交仍自行判断**，别为琐碎改动过度提交。

> 原则：**提交粒度 = 一个可独立解释、可独立回退的决策。** 既不攒大杂烩，也不把一个决策拆成一串碎提交。

---

## 2. 检查点边界的 code review 流程（codex，最多 2 轮）

每个检查点准备落"检查点提交"前，把本检查点全部改动 `git add` 后，用本机独立实例 **codex（`codex-chatgpt`，gpt-5.5 / xhigh）** 对这份 staged diff（已排除记账类，§9）做只读外审。

> **评审归口：本机 codex 是唯一的"检查点提交闸门" reviewer**（首选 `codexro-review` 直接读仓库；降级 `codex-chatgpt` 内联。见 §2.1）。
> codex 是**检查点边界的独立外审**，与 superpowers 检查点内部的子代理审核（`requesting-code-review`）**并存、互不替代**：内部审核帮你边做边把关，但**能否落检查点提交只由 codex 外审结论 + §2.2 决定**。详见 §8。

### 2.1 调用约定（本机已验证）

**根因（为什么是下面两种模式）**：本机 codex 的只读沙箱靠 bubblewrap，而**容器运行时禁了 namespace 创建**（root + 关 harness 沙箱后 `unshare -U` 仍 EPERM，非 sysctl 可改）、内核 5.4 又无 Landlock（需 ≥5.13）→ **bwrap 在本机起不来**。后果：
- codex 在 `-s read-only` 下**一旦尝试执行 shell（grep/读文件）就失败**；只能在“内容全部内联、它无需执行任何命令”时工作（模式 B）。
- 要让 codex 在推理中**自己浏览仓库**，只能关掉它的沙箱（`-s danger-full-access`）；本机用**无写权限的 `codexro` 用户**来跑，靠 Unix 文件权限保证“能读、改不动”（模式 A）。

> 两种模式共用：每次新开 `--ephemeral` session（不 resume）、`-m gpt-5.5 -c model_reasoning_effort=xhigh`、`-c approval_policy=never`、`--ignore-user-config`（关插件；auth 仍走各自 CODEX_HOME）。

#### 模式 A（首选）：`codexro-review` —— codex 直接读仓库

`/usr/local/bin/codexro-review` 以无写权限的 `codexro` 用户运行 codex（脚本内已注入代理 7890 与 `-s danger-full-access`）。reviewer 能在推理中自行 `rg`/读相关文件（调用方、类型定义、契约波及面）核实跨文件假设，**但对仓库与系统都无写权限、不在 sudoers**（已实测：写 root 路径被拒、无 sudo、能 `rg` 读到真实文件）。

```bash
# 1) 在被构建系统的 git 仓库里生成本次待审 diff（排除记账类文件，§9）
git --no-pager diff --staged -- . ':(exclude)build_log/**' ':(exclude)implement_note.md' > /tmp/review_diff.txt

# 2) 写 prompt 文件（评审指令 + 决策背景 + 内联 diff；告诉它“可自行读仓库其余文件核实”）

# 3) 跑评审：codexro-review <仓库目录> <输出文件> <prompt文件>  [追加 codex 参数]
#    <仓库目录> 须对 codexro 可读；<输出文件> 须在 codexro 可写处（如 /tmp/...）
codexro-review /path/to/repo /tmp/review_out.md /tmp/review_prompt.txt
```

- **一次性前置（只能由用户做）**：项目 `.claude/settings.local.json` 里加 `Bash(codexro-review:*)` 放行规则——agent 不能自加（auto-mode 分类器禁止自我授权放行“关沙箱”命令）。没有它，非交互运行会被拦。
- 目标仓库若在 700 路径下 codexro 读不到 → 给 codexro 读权限（加组 / ACL）。
- 凭证：codexro 的 `CODEX_HOME=/home/codexro/.codex` 仅放一份 `auth.json` 副本；若授权失效，从 root 的 CODEX_HOME 重拷 `auth.json`（root 始终是主）。

#### 模式 B（降级）：内联 + 只读沙箱

当 codexro 读不到目标仓库、或你只想要全沙箱、或 diff 很小不值得让它浏览时：把 diff（必要时加相关文件全文）**全部内联**进 prompt，用 `-s read-only` 跑 `codex-chatgpt`。**因 bwrap 不可用，prompt 必须声明“全部内容已内联、无需执行任何命令”**，否则它一 grep 就失败。

```bash
codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral \
  -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never \
  -C /tmp/scratch -o /tmp/review_out.md - < /tmp/review_prompt.txt
```

#### prompt 文件结构（两模式通用）
```
你是只读 code reviewer。下面是一次"<检查点一句话>"的改动。
审查：正确性 bug、契约破坏、并发 / 错误处理隐患、可回退性。
（模式 A：可自行 rg/读仓库其余文件核实跨文件假设；模式 B：全部内容已内联，无需执行任何命令。）
按 [BLOCKER]/[SHOULD]/[NIT] 分级，并给最终结论：APPROVE 或 REQUEST_CHANGES。

=== 决策背景 ===
<为什么这么改、影响面>

=== DIFF ===
<内联完整 diff>
```

### 2.2 两轮上限规则

- **第 1 轮**：跑评审 → 读结论。
  - 若 `APPROVE`（无 BLOCKER）→ 进入 commit。
  - 若有 BLOCKER / `REQUEST_CHANGES` → 按意见修改相关制品，进入第 2 轮。
- **第 2 轮**：把“修改后的新 diff + 第 1 轮意见 + 逐条回应”再内联送审一次。
  - 若 `APPROVE` → 进入 commit。
  - 若第 2 轮仍有 BLOCKER → **到此为止，不再回送 codex（没有第 3 轮）**。凭反馈自行判断、酌情修改后**继续往下做**：
    - 反馈确有道理 → 自己改掉，继续；
    - 反馈是误解 / 不适用 → 在提交记录里记下理由，继续。
    两种情况都**不再提交给 codex**。是否就此 commit、还是连同后续改动一并提交，按 §3 自行判断。
- 每一轮都按 §2.1 **新开 ephemeral session**，不复用上下文。

> 即“最多反复 review 2 轮”：codex 最多看 2 次；第 2 轮还不过，就凭反馈自己改、不再纠缠 codex，直接往下做。

> 收到 review 意见时：先核实、不盲从、不表演式同意。误报就反驳并记录理由；真问题才改。

---

## 3. 提交规范（git commit）

- 只提交本检查点相关的文件；无关改动另起检查点 / 提交。
- commit message 标题建议带上 ROADMAP 里的检查点编号（如 `CP1.3`），便于和 build_log / ROADMAP 对应。
- commit message 用祈使句，说清"做了什么 + 为什么"，并标注 review 状态。建议格式：

```
<类型>: <检查点一句话>

- 改动要点 1
- 改动要点 2

review: codex-chatgpt 第N轮 APPROVE（或：第2轮上限，未采纳意见X，理由…）
verify: <验证方式与结果，见 build_log/<id>.md>
```

> commit message 末尾追加当前 harness 约定的 Co-Authored-By 行。**构建会跨模型 / 跨 harness 接力，勿在此硬编码署名**——
> 一律以当前会话 harness 系统提示给出的署名行为准（harness 没给署名约定则省略该行）。

- 当前若在默认分支上做特性开发，**先开分支再改**。
- 只有在用户要求时才 `git push`。

---

## 4. 提交后必写施工记录（build_log/）—— 且记录本身必须入库

检查点提交（记为 **C**；**一个检查点一条记录**）后，立刻在 `meta_research_buiding/build_log/` 新建一条记录：

- 文件名：`NNNN-<short-slug>.md`（NNNN 为四位递增序号，如 `0007-add-retry-queue.md`）。
- 同时在 `build_log/INDEX.md` 追加一行索引（一条提交一行）。
- **记录写完后，作为紧随其后的小提交入库**：`git add build_log/ ROADMAP.md implement_note.md && git commit -m "docs(build_log): NNNN <标题>"`（连同 ROADMAP 勾选、`implement_note.md` 刷新一并提交）。
  - 日志里引用检查点提交 **C** 的 hash；**不要用 `git commit --amend`**（amend 会改掉 C 的 hash，使记录里引用的 hash 失效）。
  - **唯一例外**：这条日志提交本身**不再写 build_log**（否则无限递归）。
  - 即每个检查点落成**两个提交**：检查点提交 C + 紧随的日志提交，`git log` 上成对出现、可审计、可回退。

每条记录必须包含以下字段（用 §6 模板）：

1. **commit**：commit hash + 一句话标题；
2. **决策**：这次决定做了什么、为什么；
3. **改动文件清单**：逐个文件 + 每个文件改了什么（新增/修改/删除）；
4. **review**：第几轮过的 / 是否走满 2 轮 / 未采纳的意见及理由（附 `/tmp/review_out.md` 要点）；
5. **验证**：跑了什么命令/测试，**贴关键输出**，结论 = 通过 / 未通过 / 未验证（并说明为何未验证）；**若本检查点收尾了某一步，附跑该步「验证方法」的结果**；
6. **遗留**：已知未解问题、待办、回退方式。

> "是否验证通过"必须有**证据**（命令 + 输出），不能只写"通过"。
> 没真跑过就写"未验证"，绝不假报。

---

## 5. 完整动作清单（宏观分解 + 每个检查点；照此建 todo）

**每次新 session 开工（含换模型接手）**
```
[ ] 0. 读 implement_note.md（现场）→ ROADMAP.md（全局），从「下一步动作」接着做；
      干活中现场状态每变一档就刷新它，session 可能中断/暂停前必须写全（§9）
```

**接到目标 / 新的步（一次性 / 每步开头）**
```
[ ] A. 在 ROADMAP.md 登记用户给的步 + 该步验证方法
[ ] B. 把当前步切成检查点（superpowers 稳定长度；可边走边补，不必一次列全）
```

**每个检查点（照此循环）**
```
[ ] 1. 明确本检查点要做什么（一句话）+ 影响面；开工登记到 implement_note.md（§9）
[ ] 2. 检查点内部用 superpowers 构建（含其子代理审核 / TDD / systematic-debugging）
[ ] 3. 本地自验（编译/测试/运行；对 prompt·skill 做实读校验），留好命令与输出
[ ] 4. git add 本检查点全部改动（内部未提交，故 staged = 全量；build_log/、implement_note.md 记账类不进外审 diff，脚本已排除）；git diff --staged 导出
[ ] 5. 跑 codex 外审（第1轮，ephemeral）—— 见 §2 / bin/codex-review.sh
[ ] 6. 有 BLOCKER 则改 → 第2轮（上限）；第2轮仍不过则凭反馈自行修改、不再送 codex
[ ] 7. 落检查点提交（git commit；codex 过后）
[ ] 8. 若本检查点收尾了某一步 → 跑该步验证方法，留输出（作为 build_log 的步级验证证据）
[ ] 9. 写 build_log/NNNN（含步级验证结果）+ 更新 INDEX.md + 勾 ROADMAP.md + 刷新 implement_note.md → git commit 该记录（docs(build_log): …，引用检查点提交 hash，勿 amend）
[ ] 10. 向用户汇报：检查点提交 hash、改了什么、验证结论
```

任何一步跳过即视为本检查点未完成。

---

## 6. build_log 记录模板

```markdown
# NNNN · <检查点一句话>

- date: <YYYY-MM-DD>
- commit: <hash> — <commit title>
- branch: <branch>
- 检查点 / 步: <ROADMAP 里的 CPx.y（属：步X 名称）>

## 决策
<做了什么 / 为什么 / 影响面>

## 改动文件
- `path/to/a` — 新增：……
- `path/to/b` — 修改：……
- `path/to/c` — 删除：……

## Review（codex-chatgpt gpt-5.5/xhigh）
- 第1轮：<APPROVE / REQUEST_CHANGES + BLOCKER 列表>
- 第2轮（如有）：<结论>
- 未采纳意见及理由（如有）：……

## 验证
- 命令：`<cmd>`
- 关键输出：
  ```
  <贴输出>
  ```
- 步级验证（若本检查点收尾了某一步）：跑该步「验证方法」→ <结果>
- 结论：通过 / 未通过 / 未验证（原因）

## 遗留 / 回退
- 待办：……
- 回退：`git revert <hash>` 或 ……
```

---

## 7. 红线（任何时候都不许）

- ❌ 决策性改动长期不 commit，或把无关决策攒成一个大杂烩提交。
- ❌ **过度提交**：为琐碎改动 / 连续微调每步都单独 commit。
- ❌ 跳过检查点边界的 codex 外审，直接落检查点提交。
- ❌ 给 codex 起第 3 轮 review（最多 2 轮；第 2 轮不过就自行改、不再送审）。
- ❌ commit 后不写 build_log。
- ❌ 没真跑验证就写"验证通过"。
- ❌ 用 `--dangerously-bypass-approvals-and-sandbox` 跑 codex（会被拒，且不安全）。
- ❌ 用 superpowers 子代理审核**顶替 / 跳过检查点边界的 codex 外审**（子代理审核只是内部把关；落检查点提交的闸门只认 codex，见 §8）。
- ❌ 新 session 不读 `implement_note.md` 就开工；现场状态变了 / session 可能中断前不刷新它（§9）。

---

## 8. 与 superpowers 插件的优先级 / 协同

**优先级**：本 CLAUDE.md 属"用户显式指令"。按 superpowers 自己声明的优先级（`using-superpowers`：①用户指令 > ②技能 > ③默认行为），**任何冲突点以本文件为准**。

**评审归口（两层，本次修订）**：
- **检查点内部 = superpowers 子代理审核**：`requesting-code-review`（及 `executing-plans` 的 review checkpoints、TDD 红绿灯）**恢复启用**，作为边做边把关的"内部审核"。它提升检查点交付质量，但**不构成检查点提交许可**。
- **检查点边界 = codex 独立外审**：落"检查点提交"前，必须由本机 codex 对本检查点 staged diff（记账类除外，§9）做一次独立外审（首选 `codexro-review`；降级 `codex-chatgpt` 内联。§2.1 / §2.2）。**能否落检查点提交只由 codex 外审结论 + §2.2 决定。**
- 两者**并存、互不替代**：内部子代理审核**不能顶替也不能跳过**边界的 codex 外审（§7 红线）；codex 外审也不替你做内部把关。

**作为检查点内部的构建引擎（按需用，不是每个检查点都强制走）**：
- `brainstorming`：当某步 / 检查点本身是设计难题、意图不明时先用它对齐 —— **不强制每个检查点都走它的 HARD-GATE**；ROADMAP 里已对齐的步 / 检查点可直接进构建。
- `writing-plans` / `executing-plans` / `subagent-driven-development`：把步切成检查点、把检查点内部拆成可执行任务并推进。
- `using-git-worktrees`、`systematic-debugging`：按需。

**保留为加强项（与本约束对齐，继续遵循）**：
- `receiving-code-review`：收到 codex 意见时不盲从、先核实、不表演式同意 —— 对应 §2.2。
- `verification-before-completion`：声称"完成 / 通过"前必须真跑命令看输出 —— 对应 §4 / §7。

**TDD**：`test-driven-development` 的 test-first **不强制**；§5 的"检查点内部自验"即可（自验含跑测试）。需要某模块强制 test-first 时再单独说明。

---

## 9. 断点续作：implement_note.md（跨 session / 跨模型接力）

**目的**：任何新 session（包括换了模型的）不依赖对话记忆、只靠读文件即可无缝接手。
三份活文档分工不重叠：`ROADMAP.md` = 计划与进度骨架；`build_log/` = 已完成检查点的台账；**`implement_note.md` = 施工现场快照，只写"当下"**。

- **位置**：仓库根 `implement_note.md`。**覆盖式更新**（历史不留在本文件，去 build_log / git log 找），保持一屏内读完。
- **必须更新的时点**：
  1. 检查点开工：写目标一句话、影响面、内部计划；
  2. 现场状态每变一档（自验完 / 外审第 N 轮出结论 / 待提交 / 记账中……）就刷新；
  3. 记账时（§5 步 9）：刷新为"空闲 / 下一检查点"，随日志提交一并入库（§4）；
  4. **session 可能中断 / 暂停前**：把"改到哪、下一步动作（具体到命令/文件）、坑"写全——这是接力的生命线。
- **性质 = 记账类文件**（同 `build_log/`）：常规刷新**不算决策性改动**（§1）、**不进检查点外审 diff**（`bin/codex-review.sh` 已排除；含外审通过后、落提交前的状态刷新）。
  ⚠️ **豁免边界（防绕过外审）：常规刷新 = 只写状态、进度、下一步执行指针。** 规则、模板、检查点拆分、验收标准、取舍结论等**决策性内容必须同步落到受审制品**；`implement_note.md` 只能引用 / 摘要它们，**不能成为其唯一载体**。
- **入库时点**：检查点提交**可以包含**当时的现场快照（§5 步 4 `git add` 全量会带上它，不必刻意 unstage）；记账提交（§4）再把它刷新为「空闲 / 下一检查点」并入库。两个快照各以其时点为真，不算重复。
- **现场真相 = 工作区里的最新版**（未提交也算数）：新 session 以工作区版本为准，不必等它出现在 git 历史；git 里只会看到检查点边界时的快照。
- **新 session 开工序**：`CLAUDE.md`（约束）→ `implement_note.md`（现场）→ `ROADMAP.md`（全局）→ 需要历史再看 `build_log/INDEX.md`。

**内容模板**（覆盖式，保持精简）：

```markdown
# implement_note.md · 施工现场（活文档，只写当下）

- 更新：<YYYY-MM-DD HH:MM> ｜ 位置：<步X CPx.y ／ 治理·脚手架 ／ 空闲>
- 检查点状态：构建中 / 自验中 / 外审第N轮 / 待提交 / 记账中 / 空闲

## 正在做什么
<本检查点目标一句话 + 推进到哪>

## 工作区状态
<未提交 / 已 staged 的文件各处于什么状态；临时文件路径>

## 下一步动作（按序，具体到命令/文件）
1. <…>

## 关键上下文 / 坑（新 session 不读会踩的）
- <已定取舍的指针、临时执行指针（涉及规则 / 验收 / 取舍时，结论本体见受审制品）、已知故障及修法>
```
