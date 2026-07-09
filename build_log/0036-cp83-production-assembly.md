# 0036 · CP8.3 生产装配：bundle/judge SKILL + review_verdict 契约 + StageProvider/JudgeProvider

- date: 2026-07-09
- commit: c7863c0 — feat: CP8.3 生产装配——bundle/judge SKILL + review_verdict 契约 + StageProvider/JudgeProvider（M7）
- branch: main
- 检查点 / 步: CP8.3（属：步⑧ M7 plan 契约缺口补齐 → 全流程 real-Codex attack）

## 决策
让**真 Codex** 能产步⑧契约的制品（CP8.1/8.2 已让真组件消费它们）：
- `prompts/skills/bundle/SKILL.md` 全重写（m0-1 造假桩说明 → m7-1 真执行契约）：产 execution_manifest.json
  （照抄锚区 plan_slice_hash/协议绑定/required int）+ identity.md + 代码文件；argv 禁 shell、{src}/{ckpt}
  占位符、metric_value 输出契约、cwd 语义、失败语义（critical 早退/skipped/engineering_blocked/两段提交）。
- `prompts/skills/judge/SKILL.md` 新增：bundle_code_review/bundle_result_review 判官指令（fail 有真否决权、
  fail 须至少一条 issue、无实据不否决）。
- `schemas/review_verdict.schema.json` 新增（additive）：judge 裁决契约。
- StageProvider：bundle 阶段（required=manifest+identity.md；**passthrough 信封全量**——代码文件名任意）；
  _validate_files 支持 .md 文本；sidecar fail-loud 序保持在 passthrough 之前。
- **JudgeProvider**（judge 契约实现，attack 专用）：编排器机械装 subject 材料（DB 切片/checkpoint 哈希 +
  staging 代码全文/smoke·train·eval log 尾部）→ 独立 Codex 会话（artifact_parse 重试 + schema 校验）→
  短事务落 runner_call(audit)+DECISION(judge)（round_no 递增；policy_hash=judge SKILL sha256 版本指纹；
  subject_hash 编排器传入原样落——judge 不自算）。Codex 永不碰 DB。

## 改动文件
- `meta-research/prompts/skills/bundle/SKILL.md` — 重写（m7-1 真执行契约）。
- `meta-research/prompts/skills/judge/SKILL.md` — 新增。
- `meta-research/schemas/review_verdict.schema.json` — 新增。
- `meta-research/orchestrator/stage_provider.py` — bundle 阶段 + passthrough + JudgeProvider。
- `meta-research/orchestrator/schemas.py` — ARTIFACT_SCHEMA_MAP += review_verdict.json。
- `meta-research/orchestrator/harness.py` — 新增 latest_smoke_log（smoke-N 数值序取最新）。
- `meta-research/orchestrator/attack_stages.py` — _code_subject_hash 换用 latest_smoke_log（与 judge 材料同口径）。
- `meta-research/tests/test_stage_provider.py` — +11 测（bundle passthrough/identity/manifest 重试反馈、
  judge 落库/round 递增/非法裁决不落库/kind 白名单/smoke 数值序/result 材料含代码与全量 metric）。
- `meta-research/tests/test_skills.py` — bundle 锚词表更新（删 M0 造假锚 fake/synthetic；加
  execution_manifest/plan_slice_hash/metric_value/{src}/{ckpt} 真契约锚）。
- `meta-research/tests/{test_schemas,test_orchestrator}.py` + fixtures — review_verdict 清单锁登记 + 正负例。

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内审（Opus 子代理）：APPROVE。实证确认 SKILL↔schema/围栏/驱动逐字段一致（config_json 规则、{ckpt} 仅
  eval、metric_value 解析、cwd 语义）、judge payload 满足 review_passed 判据面、sidecar fail-loud 先于
  passthrough、staging 布局读写两侧一致。采纳 1 SHOULD + 2 NIT：①JudgeProvider docstring 收窄 attack 专用
  （import 物化布局不同，接线须另配装配器——防 CP8.4 误接）；②SKILL env 禁改清单补 PYTHONHOME；
  ③code_files 排除名单补 _staged.ok。
- codex 第1轮：REQUEST_CHANGES——2 BLOCKER + 3 SHOULD + 1 NIT，全部采纳修复：①[BLOCKER] judge SKILL 示例键名带 `?`（"fix_hint?"）会诱导 Codex 产非法 JSON（additionalProperties=false）→ 键名去 ?、加「可省键」说明与警示，连带修 bundle SKILL 的 "timeout_s"?/env?；②[BLOCKER] result review 材料缺代码与 smoke、metric 行可被 log tail 截断（判据「据结果反查代码」无据可查）→ _subject_md 两种评审均装配代码全清单+smoke，另加「metric_value 行（全量）」节（从 eval.log 全文抽取，不受截断影响）；③[SHOULD] review_kind 白名单 fail-loud（typo kind 曾会写任意 decision.type）；④[SHOULD] smoke-N.log 字典序取错「最新」（smoke-10<smoke-2）→ harness.latest_smoke_log 数值序，attack_stages subject 构造与 JudgeProvider 两侧统一换用；⑤[SHOULD] SKILL「纯文本字符串」与 manifest._to_bytes 支持 .json 对象矛盾 → SKILL 对齐既审契约（.json 值可为对象、物化为规范化 JSON）；⑥[NIT]「64 位十六进制串」→「64 个十六进制字符的 sha256 串」。各配回归测试。
- codex 第2轮：**APPROVE**（首轮 6 项逐条确认已解决；无新 BLOCKER/SHOULD；附带 1 NIT——judge SKILL result review 对象描述同步材料清单，已顺手采纳）。
- 未采纳意见及理由：无（全部采纳）。

## 验证
- 命令：`python -m pytest tests/test_stage_provider.py tests/test_skills.py -q` → 26 passed；
  `python -m pytest tests/ -q` → 608 passed。
- 关键输出：
  ```
  29 passed in 3.76s (test_skills+test_stage_provider)
  608 passed in 96.98s (0:01:36)
  ```
- 结论：通过。

## 遗留 / 回退
- 待办：CP8.4 run.py attack 全装配 + 全链 E2E + 真 Codex CLI 冒烟（步级验证核心）；接 import_worker 真
  judge 时须另配 subject 装配器（本检查点 SHOULD 注记）。
- 回退：`git revert <HASH>`。
