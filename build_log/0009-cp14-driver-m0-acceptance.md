# 0009 · CP1.4 最小驱动器 + M0 端到端验收（步① M0 收尾）

- date: 2026-07-07
- commit: 45641cc — feat: CP1.4 最小驱动器 + M0 验收
- branch: main
- 检查点 / 步: CP1.4（属：步① M0；本检查点收尾步①）

## 决策
- `orchestrator/driver.py`：M0Driver——advance 环 + route 派生（§6.13(3) attack 分支矩阵）+ idea 双 runner_call（判官只收 §3.1.3 穷举映射包）+ plan/可回答性评审环（≤2 轮意见回传修复）+ bundle 确定性造假（fake/synthetic 显式标记）+ reasoning 收尾（§4.2.5(a) 顺序）+ 失败语义分派（阶段失败=done+inconclusive；结构非法=failed+释放）+ sidecar M0 桩（校验→归档→失败观测）+ decompose 轮 activate 父问题 + transcript 全局唯一序号（P6 防覆盖）。
- `scripts/run_m0_acceptance.py`：验收⓪–④可证伪断言 + 报告（⓪全轮 done+≥3 轮+≥1 bundle target；①落盘产物逐份重校验；②context_pack manifest 来源白名单；③无任何 sqlite；④fake/synthetic 全标）。
- prompt 修订（实跑教训的制度化）：system_prompt「键名逐字/可选字段省略不写 null」铁律；idea/plan/reasoning 各调用点**JSON 输出骨架**（键名逐字模板）；bootstrap 的 selection intent 按 R3 预算规则（超预算选 decompose，修复"skill 字面指令压过一般规则"）；terminate 纪律（证据不足≠终止，只认三判据）；plan scope_spec 不双写训练配置。
- `gate.py`：schema 错误展平 oneOf 子错误（否则重试反馈无键名级定位、不收敛）。
- `input/goal_brief.md`：toy 目标补成本量级（诚实 est_cost > B0=5 → 自然触发 decompose 链路）。

## 改动文件
- `meta-research/orchestrator/driver.py` — 新增；`orchestrator/gate.py` — 修改（错误展平）
- `meta-research/scripts/run_m0_acceptance.py` — 新增
- `meta-research/tests/test_driver.py` — 新增（4 用例，ScriptedRunner 不花 token）
- `meta-research/prompts/system_prompt.md`、`prompts/skills/{idea,plan,reasoning}/SKILL.md` — 修改
- `meta-research/input/goal_brief.md` — 修改（成本提示）；`.gitignore` — 修改（questions/ 运行产物）
- `implement_note.md` — 记账随提交

## Review
- 内部（Opus 子代理）：With fixes——I1 预置 inconclusive 遇双故障留错态（根因修复：编译器可调度集含 active Qn、不再预置）、I2 收尾 ValueError 炸穿整跑（→CycleFailed 单轮失败）、I3 answer 可关错题（相等断言）、I4 skill 承诺与编译器不一致、I5 decompose_threshold 死旋钮（接线注入）；Minor：eval 假造无测试等。全部采纳。
- codex 第 1 轮：REQUEST_CHANGES——BLOCKER：合法 import_defer 使 route 矩阵悬空（M0 显式拒+decision 留痕）、manifest 不反映驱动器追加的真实输入且 bundle 无溯源（amend 唯一变异口+专用渲染器+桩 manifest，manifest 由 11 份增至 29+ 份）；SHOULD：轮型不许的 answer 落盘（commit 前拒）、rmtree 无护栏。全部采纳。
- codex 第 2 轮：REQUEST_CHANGES（达 2 轮上限）——BLOCKER：selection 必产检查晚于 commit（重排先检后 commit）、护栏用 assert 可被 -O 剥离（改显式 SystemExit）；SHOULD：收尾半途失败留过期路由指针致下轮炸穿（activate 失败→单轮 failed+停机留痕）、评审缺 normalized selected idea（补入+溯源）、判官候选一致性（重评一次仍败→CycleFailed）。全部核实采纳修复，按 §2.2 不再送第 3 轮。
- 未采纳意见及理由：无（两轮全部意见均核实为真并采纳）。

## 验证
- 单测：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q` → **105 passed**
- **步①（M0）步级验证 = 真 codex 端到端验收**（`scripts/run_m0_acceptance.py --cycles 5`，exit=0）。报告原文（运行产物在 questions/toy/，已 gitignore；transcripts/manifest 归档在该目录供回放）：
  ```
  # M0 验收报告（§7.1 M0 行）
  - 轮数: 5（c1, c2, c3, c4, c5）｜ 终止: False（跑满 5 轮上限，研究自然推进中——验收只要求 3–5 轮走通契约）
  - ①产物重校验: 27 份全过
  - ②输入溯源 manifest: 29 份全在白名单内
  - ③DB 文件: 无（0 个）
  - ④假执行标记: 8 个 target 全标 fake/synthetic
  ## 结论：通过
  ```
- 定版链路形态（逐轮）：c1 bootstrap（选 decompose，超预算规则生效）→ c2 decompose（写子题、父释放+子 dep）→ c3 attack（2 假 target，产 answer）→ c4 attack（3 假 target，产 answer）→ c5 attack（3 假 target，产 answer）→ 跑满 5 轮上限（next_intent 仍 attack，研究自然推进中）。此前迭代中亦实测过：plan 被可回答性评审真实打回一轮后修复通过、假证据下诚实 refuted、terminate 判据③触发——各分支均见过真实流量。
- 端到端共迭代 6 次（4 次到过"通过"）修掉：①skill 散文教字段→工人自造键（加输出骨架）②每调用新建 Runner 计数归 1→transcript 互相覆盖（全局序号）③bootstrap 字面"选它攻坚"压过 R3 预算规则+可调度集不含 active Qn→2 轮即 terminate ④decompose 轮未 activate 父问题的潜伏炸弹 ⑤⑥两轮 codex 外审的 BLOCKER（见 Review 节）。
- 结论：**通过**——步①（M0）全部检查点完成，M0 验收判据逐项过。

## 遗留 / 回退
- 聚合轮（child_answer 关根问题）未在本次 5 轮内出现（q2/q3 被 refuted 后工人 spawn 新题深挖，属合法路径）；其机械正确性由单测 `test_statestore_bootstrap_to_aggregate_lifecycle` 覆盖，真跑留待 M1+ 场景。
- 验收断言②的独立性注记：manifest 由编译器写（同进程），断言的是"编译器只从声明来源取数"的自我一致性 + 白名单形状；完全独立的输入溯源核查要到 M1（DECISION 入账）后由 DB 侧审计。
- M1 开工前需向用户确认：①《二》§6.12 import 旋钮 vs 附录 C 缺键；②DB evaluation.source 无 'fake' 时 M1–M3 假执行入账方式；③OPEN #1/#2。
- 回退：`git revert 45641cc`。
