# 构建大型系统 · 系统约束（CLAUDE.md）

> 本文件是**强约束（hard constraints）**，不是建议。在本目录及其子目录下进行任何系统构建工作时，
> 下列规则 **OVERRIDE 默认行为**，必须逐条照做。违反任何一条 = 本次工作未完成。

适用范围：本目录（`meta_research_buiding/`）所看护的大型系统的全部代码改动。
施工日志统一落在本目录下：`meta_research_buiding/build_log/`。

---

## 0. 三条铁律（先读这三条，其余是展开）

1. **决策性代码改动要及时 git commit——但是否提交、粒度大小自行判断。** 不要把无关决策攒成一个大杂烩提交；也**不要过度提交**（琐碎改动、连续微调不必每步单独成 commit，攒到一个有意义的决策点再提）。
2. **每次决定要 commit 之前 → 必须先调用本机 `codex-chatgpt` 做 code review，最多反复 2 轮。**
   review 未过且仍未到 2 轮上限，**不许 commit**。
3. **每次 commit 之后 → 必须在 `build_log/` 写一条提交记录**：改了哪些文件、做了什么、是否验证通过。

任意一条没做，这次改动就**不算完成**，不要向用户声称"已完成 / 已提交 / 已验证"。

---

## 1. 什么算"决策性改动"（必须走完整流程）

走完整流程（review → commit → 记录）的改动包括但不限于：

- 新增 / 删除 / 重命名模块、接口、数据结构、配置项；
- 改变控制流、算法、并发模型、错误处理策略；
- 改变对外契约（API、CLI、文件格式、DB schema）；
- 引入 / 升级 / 移除依赖；
- 任何会影响别处行为、或难以回退的改动。

**不算**决策性改动、可不单独走流程的：纯排版、注释错别字、本地实验性涂鸦（未进主干、随手丢弃）。
拿不准某改动算不算“决策性”时，倾向当决策处理；但**是否单独成一次提交仍自行判断**，别为琐碎改动过度提交。

> 原则：**提交粒度 = 一个可独立解释、可独立回退的决策。** 既不攒大杂烩，也不把一个决策拆成一串碎提交。

---

## 2. 提交前的 code review 流程（codex-chatgpt，最多 2 轮）

每次准备 commit 前，用本机独立实例 **`codex-chatgpt`（gpt-5.5 / xhigh）** 做只读评审。

> **评审归口：本机 codex 是唯一的 pre-commit reviewer**（首选 `codexro-review` 直接读仓库；降级 `codex-chatgpt` 内联。见 §2.1）。
> **不调用** superpowers 的 `requesting-code-review`（它会起 Claude 子代理评审）——本流程用 codex 评审**替代**它。详见 §8。

### 2.1 调用约定（本机已验证）

**根因（为什么是下面两种模式）**：本机 codex 的只读沙箱靠 bubblewrap，而**容器运行时禁了 namespace 创建**（root + 关 harness 沙箱后 `unshare -U` 仍 EPERM，非 sysctl 可改）、内核 5.4 又无 Landlock（需 ≥5.13）→ **bwrap 在本机起不来**。后果：
- codex 在 `-s read-only` 下**一旦尝试执行 shell（grep/读文件）就失败**；只能在“内容全部内联、它无需执行任何命令”时工作（模式 B）。
- 要让 codex 在推理中**自己浏览仓库**，只能关掉它的沙箱（`-s danger-full-access`）；本机用**无写权限的 `codexro` 用户**来跑，靠 Unix 文件权限保证“能读、改不动”（模式 A）。

> 两种模式共用：每次新开 `--ephemeral` session（不 resume）、`-m gpt-5.5 -c model_reasoning_effort=xhigh`、`-c approval_policy=never`、`--ignore-user-config`（关插件；auth 仍走各自 CODEX_HOME）。

#### 模式 A（首选）：`codexro-review` —— codex 直接读仓库

`/usr/local/bin/codexro-review` 以无写权限的 `codexro` 用户运行 codex（脚本内已注入代理 7890 与 `-s danger-full-access`）。reviewer 能在推理中自行 `rg`/读相关文件（调用方、类型定义、契约波及面）核实跨文件假设，**但对仓库与系统都无写权限、不在 sudoers**（已实测：写 root 路径被拒、无 sudo、能 `rg` 读到真实文件）。

```bash
# 1) 在被构建系统的 git 仓库里生成本次待审 diff
git --no-pager diff --staged > /tmp/review_diff.txt

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
你是只读 code reviewer。下面是一次"<一句话决策>"的改动。
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
  - 若有 BLOCKER / `REQUEST_CHANGES` → 按意见改代码，进入第 2 轮。
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

- 只提交本次决策相关的文件；无关改动另起提交。
- commit message 用祈使句，说清"做了什么 + 为什么"，并标注 review 状态。建议格式：

```
<类型>: <一句话决策>

- 改动要点 1
- 改动要点 2

review: codex-chatgpt 第N轮 APPROVE（或：第2轮上限，未采纳意见X，理由…）
verify: <验证方式与结果，见 build_log/<id>.md>
```

> commit message 末尾按 harness 约定追加：
> `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

- 当前若在默认分支上做特性开发，**先开分支再改**。
- 只有在用户要求时才 `git push`。

---

## 4. 提交后必写施工记录（build_log/）—— 且记录本身必须入库

代码提交（记为 **C**）后，立刻在 `meta_research_buiding/build_log/` 新建一条记录：

- 文件名：`NNNN-<short-slug>.md`（NNNN 为四位递增序号，如 `0007-add-retry-queue.md`）。
- 同时在 `build_log/INDEX.md` 追加一行索引（一条提交一行）。
- **记录写完后，作为紧随其后的小提交入库**：`git add build_log/ && git commit -m "docs(build_log): NNNN <标题>"`。
  - 日志里引用代码提交 **C** 的 hash；**不要用 `git commit --amend`**（amend 会改掉 C 的 hash，使记录里引用的 hash 失效）。
  - **唯一例外**：这条日志提交本身**不再写 build_log**（否则无限递归）。
  - 即每个决策落成**两个提交**：代码提交 C + 紧随的日志提交，`git log` 上成对出现、可审计、可回退。

每条记录必须包含以下字段（用 §6 模板）：

1. **commit**：commit hash + 一句话标题；
2. **决策**：这次决定做了什么、为什么；
3. **改动文件清单**：逐个文件 + 每个文件改了什么（新增/修改/删除）；
4. **review**：第几轮过的 / 是否走满 2 轮 / 未采纳的意见及理由（附 `/tmp/review_out.md` 要点）；
5. **验证**：跑了什么命令/测试，**贴关键输出**，结论 = 通过 / 未通过 / 未验证（并说明为何未验证）；
6. **遗留**：已知未解问题、待办、回退方式。

> "是否验证通过"必须有**证据**（命令 + 输出），不能只写"通过"。
> 没真跑过就写"未验证"，绝不假报。

---

## 5. 每次决策性改动的完整动作清单（照此 TodoWrite）

```
[ ] 1. 明确本次决策（一句话）+ 影响面
[ ] 2. 改代码
[ ] 3. 本地自验（编译/测试/运行），留好命令与输出
[ ] 4. git add 本次相关文件；git diff --staged 导出
[ ] 5. 跑 codex-chatgpt 评审（第1轮，ephemeral）
[ ] 6. 有 BLOCKER 则改 → 第2轮评审（上限）；第2轮仍不过则凭反馈自行修改、不再送 codex
[ ] 7. 按需 git commit（是否提交 / 粒度自行判断）
[ ] 8. 写 build_log/NNNN 记录 + 更新 INDEX.md → git commit 该记录（docs(build_log): …，引用代码提交 hash，勿 amend）
[ ] 9. 向用户汇报：commit hash、改了什么、验证结论
```

任何一步跳过即视为本次未完成。

---

## 6. build_log 记录模板

```markdown
# NNNN · <一句话决策>

- date: <YYYY-MM-DD>
- commit: <hash> — <commit title>
- branch: <branch>

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
- 结论：通过 / 未通过 / 未验证（原因）

## 遗留 / 回退
- 待办：……
- 回退：`git revert <hash>` 或 ……
```

---

## 7. 红线（任何时候都不许）

- ❌ 决策性改动长期不 commit，或把无关决策攒成一个大杂烩提交。
- ❌ **过度提交**：为琐碎改动 / 连续微调每步都单独 commit。
- ❌ 跳过 codex-chatgpt review 直接 commit。
- ❌ 给 codex 起第 3 轮 review（最多 2 轮；第 2 轮不过就自行改、不再送审）。
- ❌ commit 后不写 build_log。
- ❌ 没真跑验证就写"验证通过"。
- ❌ 用 `--dangerously-bypass-approvals-and-sandbox` 跑 codex（会被拒，且不安全）。
- ❌ 用 superpowers 的 Claude 子代理评审顶替 / 跳过 codex-chatgpt（评审归口只认 codex，见 §8）。

---

## 8. 与 superpowers 插件的优先级 / 协同

**优先级**：本 CLAUDE.md 属"用户显式指令"。按 superpowers 自己声明的优先级（`using-superpowers`：①用户指令 > ②技能 > ③默认行为），**任何冲突点以本文件为准**。

**评审归口（本次决定）**：
- 提交前评审**只用本机 codex**（首选 `codexro-review` 直接读仓库；降级 `codex-chatgpt` 内联。§2.1 / §2.2）。
- **停用** superpowers 的 `requesting-code-review`（它会起 **Claude 子代理**评审）——不在提交关卡触发；若它被自动唤起，按本文件改走 codex 评审替代。
- 仍可手动用 Claude 子代理做"非关卡"的辅助阅读，但**它不构成提交许可**；能否 commit 只由 codex 评审结论 + §2.2 决定。

**保留为加强项（与本约束对齐，继续遵循）**：
- `receiving-code-review`：收到 codex 意见时不盲从、先核实、不表演式同意 —— 对应 §2.2。
- `verification-before-completion`：声称"完成 / 通过"前必须真跑命令看输出 —— 对应 §4 / §7。

**正交、各管各（不冲突，照常用）**：`brainstorming`、`writing-plans` / `executing-plans`、`using-git-worktrees`、`systematic-debugging`。

**TDD**：`test-driven-development` 的 test-first **不强制**；§5 的"改代码 → 自验"即可（自验含跑测试）。需要某模块强制 test-first 时再单独说明。
