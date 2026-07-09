# 0046 · CP10.1 runner 成本捕获（步⑩ M6 成本记账·第一关）

- date: 2026-07-09
- commit: 1b415f9 — feat: CP10.1 runner 成本捕获（步⑩ M6 成本记账·第一关）
- branch: main
- 检查点 / 步: CP10.1（属：步⑩ M6 硬化 成本记账接线）

## 决策
**做了什么**：步⑩第一关——让 runner 捕获每次 LLM 调用的真实用量（token + 墙钟），挂到 `Artifact.usage`。
这是激活休眠的全局预算安全网（`budget_exhausted`）的前置：CP10.2 才用 usage 写 ledger。

**为什么这么做 / 关键勘察**（Explore 代理 + 实机 probe 定论）：
- 读侧已装（`stopcontroller` 读 `SUM(ledger.money)≥session_max`），但无任何 `INSERT INTO ledger` → SUM 恒 0、安全网休眠。
- **token 源实机确认**：生产 runner 用的 `codex-chatgpt exec` 把「`tokens used`\n`<N>`」（N=逗号分隔**总** token）打到
  **stderr**（stdout=信封）；runner `capture_output=True` 已捕获 stderr、但只在失败时读、成功时丢弃 → 解析即得真 token。
- 本关纯捕获、不落库、**不改循环行为**（低风险、可独立解释/回退）。

**影响面**：interfaces.py（加类型）+ runner.py（捕获）+ 测试。不碰编排/循环/DB。

## 改动文件
- `meta-research/orchestrator/interfaces.py` — 加 `CallUsage`（tokens_total/tokens_input/tokens_output/wallclock_sec，
  codex 只报总 token → input/output 置 0）+ `Artifact.usage: Optional[CallUsage] = None`（可选末字段，非破坏）。
- `meta-research/orchestrator/runner.py` — `parse_tokens_used(stderr)`：正则**行首锚定** `^[ \t]*tokens used`（不吃
  `cache/prompt tokens used`）+ 分隔符必需 + **合法千分组/纯数字** `(\d{1,3}(?:,\d{3})+|\d+)` + 行尾锚定（拒 `1,abc`
  半解析 / `1,2,3` 非法分组）+ 多次出现取**末条**汇总；坏输入/缺失 →0（健壮）。`_invoke` 成功路径 `time.monotonic`
  计墙钟 + 解析 stderr → `CallUsage`，`run_task` 挂 `Artifact.usage`。用量**只成功路径**捕获（失败→raise 不记账）。
- `meta-research/tests/test_run.py` — 删既有测试污染行 `monkeypatch.setattr(R.CodexRunner, "__new__", …)`：
  `CodexRunner.__new__` 继承自 object，monkeypatch 撤销时把 `object.__new__` **显式绑到类上**，使之后任何
  `CodexRunner(**kw)` 抛 `TypeError: object.__new__() takes exactly one argument`（污染全局）。本检查点新测是**首个**
  在 test_run.py 之后真正构造 CodexRunner 的用例、首次触发；该 patch 是冗余防御（真 runner 不构造已由同函数下方 mock
  build_system 保证）→ 删除 + 注释。
- `meta-research/tests/test_runner_usage.py` — 新增（15 例）：`parse_tokens_used` 真格式 + 严格性坏例
  （`1,abc`/`1,2,3`/内嵌标签/多次取末/无分隔符粘连）+ mock 子进程集成（usage 捕获 / 无 token 行→0 / 失败仍 raise）。

## Review（codex-chatgpt gpt-5.5/xhigh；两轮上限 §2.2）
- 第1轮：**REQUEST_CHANGES**。2 SHOULD（正则太松：`1,abc`→1、`1,2,3`→123 半解析/非法分组；未锚定标签误吃
  `cache tokens used`）+ 1 NIT（`_invoke` 返回标 `tuple`→`tuple[str,CallUsage]`）——**全改**（合法千分组 + 行首/行尾锚定
  + 取末条）。另：第1轮后跑全量暴露上述 test_run.py 既有污染 bug，一并修。
- 第2轮：**APPROVE**（无 BLOCKER/SHOULD）。1 NIT（分隔符全可选、`tokens used123` 会被接受）——**已补**（分隔符必需）。
- 未采纳意见及理由：无（全采纳）。

## 验证
- 命令：`python -m pytest -q`
- 关键输出：
  ```
  678 passed in 123.01s      # 全量（NIT 分隔符收紧前）
  15 passed                  # test_runner_usage（NIT 后；正则为隔离改动，仅本测覆盖）
  ```
  污染修复实证：`pytest tests/test_run.py tests/test_stage_provider.py tests/test_runner_usage.py` → 56 passed（修前 3 fail）。
- 步级验证（步⑩未收尾）：本关不收尾步⑩（CP10.3 收口），不跑步级验证。
- 结论：**通过**。

## 遗留 / 回退
- 待办：**CP10.2**（ledger 写入 + 激活安全网）：`CostLedger` 服务写 `runner_call`（补全 idea/plan/bundle/reasoning 各 phase）
  + `ledger`（tokens/wallclock/money）；policy 加 `price_per_1k_tokens`（money=tokens/1000×rate，notional）+ schema；
  装配进 StageProvider/JudgeProvider。→ `SUM(ledger.money)` 随真用量长 → `budget_exhausted` 转活。
  **CP10.3**：status_card `cycle_spent`←`SUM(ledger)` 对账 + budget_exhausted 端到端真触发（步级收口）。
- 已知欠计：失败/超时的 LLM 调用不记账（量小、由重试主导）；codex 只报总 token（input/output 置 0）。
- 回退：`git revert 1b415f9`（加类型 + 捕获 + 测试污染修，无循环耦合，安全）。
