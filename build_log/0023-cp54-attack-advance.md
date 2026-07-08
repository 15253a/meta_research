# 0023 · CP5.4 attack 轮 advance 全链——首次全链跑通

- date: 2026-07-08
- commit: 6af22cf — feat: CP5.4 attack 轮 advance 全链——首次全链跑通（M4）
- branch: main
- 检查点 / 步: CP5.4（属：步⑤ M4 真执行 + 真 log + import 物化）

## 决策
把 CP5.1–5.3 的 gates/harness/parser 接成**可跑的 attack 轮**——本检查点后系统**首次全链跑通**：
bootstrap 创世 → attack（idea 候选全量入 IDEA 表 → plan 单事务落 build_target+required+池占位 → bundle 真子进程
训练/评估 + 双机械评审 + 测量注册 + 入池 → reasoning 以真 metric_result 证据关问）→ terminate。

架构要点（attack_stages.py，Advancer 委托）：
- **游标**：cycle.status = 最后已提交阶段（created→idea→plan→bundle→done）；kill-9 重启从游标续。
- **两段提交（§4.2.5）**：(i) 执行事实随发生短事务；(ii) 注册段 = 可恢复短事务序列（测量注册原子性由
  gate_register_evaluation 单事务保证；整段合一事务 = M5/M6 硬化，诚实注记）。
- **崩溃恢复结构化**（本检查点两层评审的主战场，见 Review）：每目标进度从 DB 状态重导出；log 补登幂等无条件
  （从 staging 存活文件重导出 + **sha256 锚强校验**）；eval final 存活 → 续注册不重跑（exit 侧车同判）；
  judge replay-safe；reasoning 产物先原子持久化。
- **管线强制**：complete 前核 attempt 当前口径 parser 观测（防「无据不疑」绕过）；smoke exit≠0 → failed(smoke)；
  answer 只许关本轮 Qn；无 answer 轮 Qn→inconclusive（§4.2.3「阶段失败=轮正常收尾」）。
- **route 特化 deferral（跟踪）**：起手即 attack（plan 现只落 build 目标故正确）；eval_only/reuse_only/
  dependency_wait 特化随对应 plan 形态接入（advancer docstring 注明）。

## 改动文件
- `meta-research/orchestrator/attack_stages.py` — 新增（核心 ~380 行）。
- `meta-research/orchestrator/phase_commit.py` — 新增：check_or_record（事务内）+ SqlitePhaseCommit（独立短事务）。
- `meta-research/orchestrator/advancer.py` — 修改：attack 接线 + run_cycles 逐格内循环 + 8 格进度护栏。
- `meta-research/orchestrator/harness.py` — 修改：cwd=staging + exit 侧车（先于 final 改名）。
- `meta-research/orchestrator/gate_pool.py` — 修改：gate_register_evaluation 加 artifact_ref 锚参 + 组合器注记对齐。
- `meta-research/orchestrator/obs_parser.py` — 修改：加 suspect_attempt_has_current_obs（管线强制点用）。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图两行。
- `meta-research/tests/test_attack_advance.py` — 新增：12 测。

## Review（本检查点两层评审共抓 5 BLOCKER——全部是崩溃缝隙/防篡类，全采纳）
- **内审（Opus）**：REQUEST_CHANGES → 全修。**BLOCKER**（实证复现）：崩在 gate_register_evaluation（已提交）与
  eval log ingest 之间 → resume 永不补登 → 强制核永 raise → target 永卡 running（不可恢复楔死）→ 补登抽
  `_register_and_ingest_log` 幂等无条件跑（train log 同修，否则杀/不杀观测数不一致）。SHOULD：bundle pc hash
  锚终态非产物集 → 诚实降级「完成标记」注记；route deferral 跟踪；覆盖补注册-补登缝隙测 + failed-train 测 +
  _final_state 补 run/log/obs 三表。语义修复：无 answer 轮 Qn 永卡 active → mark_inconclusive。
- **外审（codex gpt-5.5/xhigh）第1轮**：REQUEST_CHANGES → 全修。**BLOCKER×2**：① 崩在「eval final 已改名→
  register 前」→ 重跑撞同名 final 拒 → 永久楔死 → 从存活 final 续注册；② eval log 补登无哈希锚 → staging
  改写可把 suspect 洗成 clean → artifact_ref=sha256 锚 + 补登强校验（篡改测试证 fail loud）。SHOULD×4：
  ckpt_key 带 run id（UNIQUE 撞楔死）/smoke exit code/reasoning 产物先持久化（防 close↔atomic 缝隙重调非
  确定 provider）/answer.question_id 绑定本轮 Qn。NIT：进度护栏。
- **外审第2轮**（**2 轮上限**，按 §2.2 凭反馈自行修复后提交、不再送审）：**BLOCKER×2**：① eval-final 续注册
  把 exit_code 固定 0——失败进程输出合法 metrics 会被续注册成成功 → harness exit 侧车（先于 final 改名，
  final 在 ⟹ 退出码可读）+ resume 同一判定点 + 回归（撒谎 eval 杀/不杀终库一致且绝不注册）；② smoke 失败
  早退绕过 _ensure_target_pc → 终态早退也落 pc + 回归。SHOULD×2：① artifact_ref=None 放行 = append/repro
  同洞 → **无锚不 ingest**（fail closed）；② judge 崩后重调非确定 → _judge_once 按 (target,kind,subject_hash)
  复用既有 DECISION + 回归（崩后续跑 code review 不重调）。
- 未采纳意见：无（三轮全采纳；第2轮依上限规则自行修复入本提交）。

## 验证
- 命令：`pytest tests/ -q`
- 关键输出：
  ```
  421 passed in 54.90s
  ```
- 12 attack 测含：全链 e2e（真 subprocess 训练/评估）、真 kill-9 阶段边界恢复、注册-补登缝隙恢复、eval-final
  缝隙恢复、失败 eval 侧车同判（杀/不杀终库一致）、篡改洗白拒、smoke 失败+pc、judge replay、failed-train
  干净收尾、pc conflict、强制 ingest。
- 结论：通过。（CP5.4 未收尾步⑤；M4 步级验证在 CP5.6。）

## 遗留 / 回退
- 待办：CP5.5 import 物化（OPEN #6 落地；gate_start_build_target/register_evaluation 两处 import
  NotImplementedError 在此接）；CP5.6 语义判据 5 判例 + M4 步级验证收尾。M5/M6 硬化项：注册段合一事务、
  bundle pc 产物集哈希锚、route plan 后特化（eval_only/reuse_only/dependency_wait）、评估产物规范 artifact。
- 回退：`git revert 6af22cf`（新模块+接线；M0 driver/M3 reasoning-only 路径不受影响，回退不破基线绿）。
