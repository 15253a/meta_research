# 0030 · CP7.2 StageProvider——真 Codex→真组件阶段回调（M6）

- date: 2026-07-08
- commit: 2ce750e — feat: CP7.2 StageProvider 真 Codex 生产装配（M6）
- branch: main
- 检查点 / 步: CP7.2（属：步⑦ M6 长跑 + 验收剧本）

## 决策
让真组件 + 真 Codex 端到端跑起来的适配层。M0 driver 用真 Codex 但跑桩栈（M0 验收栈）；M3+ 真组件
（SqliteAdvancer/AttackStages）消费注入式 provider 回调（生产=真 Codex、测试=替身）——生产回调一直缺。
本检查点补 **StageProvider**：把 CodexRunner 一次会话 + 信封解析 + 逐产物 schema 校验 + artifact_parse
重试封成组件期望的 (cyc,pack)→files 签名。

- **产文件三回调**：idea/plan/reasoning（judge 写 DB 形态不同、bundle 由 plan TARGET_SPEC 经 harness
  驱动——均非本类，留 CP7.4）。
- **pack 由调用方渲染**（不重渲）；重试把上次 schema 错误追加进 skill（自足反馈，不依赖 SqliteCompiler
  .amend——其无此方法）。信封异常一律 RunnerError（_parse_envelope），故只 catch RunnerError。
- **职责边界**：只保证「产结构合法 files」；语义由组件把关。关键：不判 driver 的 answer_allowed——
  advancer.reasoning-only 轮不读 answer.json；attack 轮读 answer 但校 question_id + 委托 close_gate
  的 I3 证据闸（幻觉 answer 关不掉）。故 answer_allowed 冗余（内审实证 + 回归）。
- **sidecar fail-loud**：阶段产 resource_request.json → 本路径未接文件请求桥 → fail loud RunnerError，
  不静默丢弃（接线 CP7.4）。

## 改动文件
- `meta-research/orchestrator/stage_provider.py` — 新增：StageProvider（idea/plan/reasoning + _produce
  重试核心 + _validate_files + _schema_errors + sidecar fail-loud）。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图加 stage_provider.py。
- `meta-research/tests/test_stage_provider.py` — 新增：12 测（reasoning/idea/plan 直测过 schema[真
  fixture]/optional 透传/必产缺失重试/schema 非法重试附反馈/重试用尽 RunnerError/RunnerError 计入重试/
  transcript 唯一/sidecar fail-loud/幻觉 answer 不误关 e2e/真 SqliteAdvancer e2e 跑通）。

## Review
- 内审（Opus 子代理）：APPROVE。改动应内审 SHOULD：sidecar fail-loud（加）；dead compiler 字段删；补
  idea/plan 直测 + 幻觉 answer e2e。answer.json 语义边界（#2 最尖锐点）实证非缺陷：advancer 忽略 answer、
  attack 轮经 gate I3 闸——「gate 过则 target 已 complete 则非 stage-failed」，无 driver guard 会挡而 gate
  不挡的场景。**记 2 个 CP7.4 前置**（预存 M4 漂移，非本检查点引入）：①idea_set.schema 无 content_md 但
  attack_stages._idea_stage 读 c["content_md"]——过 schema 的候选会让消费者 KeyError；②stage-sidecar→
  notify.create_file_request 桥（本次 fail-loud 占位）。
- codex（gpt-5.5/xhigh）第1轮：**APPROVE**（无 BLOCKER；契约边界全对——required/optional 校验、
  RunnerError 重试、schema feedback、sidecar fail-loud、answer 语义下沉组件）。SHOULD（stage 漂移未校验→
  文件对但 envelope stage 错会削弱审计/回放）+ NIT（错误截断 [:300] 可能截掉 oneOf 字段路径）均采纳：
  加 art.stage==stage 校验计入重试 + 回归；错误不截断。第1轮即 APPROVE，无第2轮。
- 未采纳意见及理由（如有）：无。

## 验证
- 命令：`python -m pytest tests/test_stage_provider.py -q` → **13 passed**；`python -m pytest tests/ -q`
  → **515 passed**（CP7.1 后基线 502 + 13 新，无回归）。
- 结论：通过。

## 遗留 / 回退
- **CP7.4 硬前置（内审记）**：①attack_stages._idea_stage 读 c["content_md"]/audit_score 与冻结
  idea_set.schema（core_claim/mechanism/audit_mapping/novelty_* + wildidea_extra…）不符——接 idea 到真
  Codex 前必须校准消费者↔schema（否则首个真 idea 轮 KeyError）；②stage-emitted sidecar → 文件请求桥。
- 待办（M6 续）：CP7.3 全系统装配入口 run.py（goal_brief→DB→装配真组件+StageProvider+CodexRunner→
  run_cycles）+ 会话双模式 A/B + kill-9 真栈恢复冒烟；CP7.4 judge provider + §7.3 机制验收剧本 + M6 步级验证。
- 回退：`git revert 2ce750e`（stage_provider 独立新模块，回退不波及既有）。
