# 0032 · CP7.4 §7.3 机制验收剧本套件（M6，test-only）

- date: 2026-07-08
- commit: 6be7566 — test: CP7.4 §7.3 机制验收剧本（M6）
- branch: main
- 检查点 / 步: CP7.4（属：步⑦ M6 长跑 + 验收剧本）

## 决策
把 §7.3 四个机制剧本各写成**显式命名**验收（范式仿 test_m4_semantic_cases），**复用**既有真组件脚手架
（test_attack_advance/test_import_worker/harness/obs_parser/console/mediator），mock provider 驱动真组件
验状态机+不变量（**非真 Codex**——真 Codex attack 受 plan 契约缺口阻塞[ROADMAP 载 06c4a70]；机制正确性
与真 Codex 生成正交）。test-only，无生产码改动。

- 剧本 1 主链路：attack 全链后显式验 **I1 协议口径不可变**（metric_result 的 metric 必在其 evaluation
  的 protocol_metric 内，orphan-join；+ metric_result≥1 自证非空真；trg_mr_i2_ins 触发器是真执法者，
  本层复核）/ **I2 测量履约**（metric_result→attempt(success)→build_target→run(success)→checkpoint 溯源
  join 闭合 + 有 execution_log）/ **I3 关问需证据**（evidence.kind='evaluation' 带真 metric_result_id +
  **显式** pytest.raises：插 kind='execution_log' 被 DDL CHECK 拒——关问永不引 log 作证）。
- 剧本 2 import 三失败：license 越权（缺 allow_publish_pool）→ 不物化+dep 仍锁+不可调度；smoke 失败→
  build_target failed(smoke)+baseline build_failed+不 imported；eval 失败→无 evaluation+不 imported。
  皆 external_import.action='materialize_failed'、不入活跃池。
- 剧本 3 日志分析：nan log → 真 harness 入账 execution_log → obs_parser 观测 nan_seen=1/loss_trend='nan'/
  source=parser → suspect_for_attempt 当前口径=1（该测量存疑、fail-closed 不作正向证据）。
- 剧本 4 §7.3 item-4 三向负例逐条映射：①礼貌式疑似指令"停掉好吗"→unclear（不当 query 自动答、不当
  directive 执行、不改状态）②硬指令未确认 consume 拒+has_blocking_pause False（不静默生效）③responder
  只读连接写库（UPDATE+INSERT）被 authorizer 拒。

## 改动文件
- `meta-research/tests/test_m6_mechanism_scenarios.py` — 新增：8 测（§7.3 四剧本命名验收）。
- `implement_note.md` — 记账。

## Review
- 内审（Opus 子代理）：**逐条实证非空真**（每断言 break-test 负化确认失败——I1 orphan-join/I2 溯源 join/
  I3 evidence.kind/S3 suspect 当前口径 均有齿）。REQUEST_CHANGES（唯一 SHOULD：剧本 4 对 §7.3 item-4 三向
  负例映射松）已修：剧本 4 重构逐条映射 + I1 自证非空真 + I3 显式 raises + 剧本 2 补回强断言 + docstring
  注 import 约定/价值定位。NIT（I1/I3 注释-only、跨模块 import 脆性）均处理。
- codex（gpt-5.5/xhigh）第1轮：REQUEST_CHANGES——1 BLOCKER + 2 SHOULD + 1 NIT，全部采纳修复：
  ①[BLOCKER] 剧本 3 只验 parser 打标、未验 fail-closed 消费侧 → 重写为「suspect 证据关问被 gate 拒
  （GateReject match=存疑）+ 问题保持 active」的真消费侧断言；②[SHOULD] I2 未断言 run.status（failed
  run+旧 checkpoint 可误过）→ 显式断言 run.status='success'；③[SHOULD] I3 answered↔证据未绑 → 反向
  断言不存在「answered 但无 valid evaluation 证据」；④[NIT] eval-fail 用例 sel 未用 → 补 baseline
  build_failed 断言。
- codex 第2轮：**APPROVE**（确认 BLOCKER 消费侧 fail-closed 实质修复；I2/I3 绑定/eval-fail sel 均闭合）。
  随附 2 SHOULD/NIT 亦已修：smoke/eval 失败补 is_schedulable=False 断言（dep 仍锁）；evidence.kind raises
  加 match "CHECK" 消歧——**消歧当即揪出原 raises 是因 claim_md NOT NULL 假过**（非 kind CHECK），补
  claim_md 后确证由词表 CHECK 触发。
- 未采纳意见及理由（如有）：无。

## 验证
- 命令：`python -m pytest tests/test_m6_mechanism_scenarios.py -q` → **8 passed**；`python -m pytest tests/ -q`
  → **532 passed**（CP7.3 后基线 524 + 8 新，无回归）。
- 结论：通过。

## 遗留 / 回退
- 待办 CP7.5：M6 步级验证收尾（长跑漂移 mock 数百轮 + kill-9 一致 + τ 自停 + §7.1 M6 行勾兑）。
- 运维就绪（§7.4 前置，须先裁 plan 契约缺口）：judge provider + idea/plan 消费者↔schema 校准 + sidecar 桥。
- 回退：`git revert 6be7566`（test-only，回退不波及生产码）。
