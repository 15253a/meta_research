# 0007 · CP1.2 流程层：system_prompt + 四阶段 SKILL + 过程 schema

- date: 2026-07-07
- commit: 9ee4c45 — feat: CP1.2 流程层
- branch: main
- 检查点 / 步: CP1.2（属：步① M0）

## 决策
落 M0 流程层（措辞即行为的决策性制品）：
- `prompts/system_prompt.md`：无状态阶段工人角色锁定（五铁律：无状态/只产产物/无 shell/落盘语言/诚实纪律）+ 输出信封协议（单个 ```json 块 `{"files","md"}`，已真机探针验证一次调用即合规）。
- `prompts/skills/{idea,plan,bundle,reasoning}/SKILL.md`：
  - idea = 双 runner_call（生成 phase=idea + 独立判官 phase=audit，判官输入 = §3.1.3 穷举映射包）；NEED 分支；novelty 固定文本逐字给出（schema const）；
  - plan = 复用判定五情形表（情形→kind→必填字段，含 claim/eval 三件套逐字段）+ 锁协议逐字段清单 + 可回答性评审（独立 ≤2 轮）+ 聚合轮/依赖等待分支；
  - bundle = KIND 分支 + 双评审 + 两段提交 + RFAIL 分流（failed 恒携事实、skipped 仅未执行旁路——对 06f 状态机的口径裁定，见下）+ M0 驱动器代跑边界；
  - reasoning = 轮型分派（bootstrap/decompose/goal_amend + R1–R4）+ evidence 四分支精确键 + create_root local_key 锚（bootstrap selection 回指）。
- `schemas/idea_audit.schema.json`（跨文件 $ref 复用 idea_set.$defs.audit_score）+ `schemas/plan_review.schema.json`（fail⇒issues≥1）。
- `schemas/tree_ops.schema.json`：create_root 增可选 local_key；`schemas/bundle_target.schema.json`：complete ⇒ 双评审占位必带（verdict=pass）。
- tests：conftest 加 referencing.Registry；test_skills.py（骨架六要素+锚词+schema 引用存在性+中文占比）；正负例增 idea_audit/plan_review/bootstrap selection/create_evaluation/complete_missing_review。

**口径裁定（规范内部措辞松动）**：图 05/§3.4「非 critical→skipped」与 06f 状态机（running→failed；pending→skipped）字面冲突；按 DDL/06f 采「已执行失败恒 failed（成败同记），仅未执行旁路 skipped」，schema 已焊死（skipped 不携执行事实）。

## 改动文件
- `meta-research/prompts/system_prompt.md` — 新增
- `meta-research/prompts/skills/{idea,plan,bundle,reasoning}/SKILL.md` — 新增
- `meta-research/schemas/{idea_audit,plan_review}.schema.json` — 新增
- `meta-research/schemas/{tree_ops,bundle_target}.schema.json` — 修改（local_key / complete⇒评审）
- `meta-research/tests/{conftest,test_schemas,test_skills}.py` + fixtures — 修改/新增
- `implement_note.md` — 记账随提交

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内部（superpowers 子代理，Opus 4.8 按用户指示）：With fixes——Critical：plan skill 未教 target 必填字段（target_key/spec_md/claim）；Important：判官输入越出 §3.1.3 映射包（已收窄至穷举清单）、bootstrap selection 无锚（create_root 增 local_key）；Minor 4 条全部采纳。
- codex 第 1 轮：REQUEST_CHANGES——BLOCKER：plan metric_defs 键名错位 / bundle skipped-failed 混写 / reasoning evidence 键未点名；SHOULD：complete 未强制评审占位；NIT：六要素只测五。全部采纳修复。
- codex 第 2 轮：REQUEST_CHANGES（达 2 轮上限）——BLOCKER：idea_set.draft 无 schema 可校（增 idea_set_draft.schema.json，$ref 复用）、skipped 禁得不全（补禁 failure_kind/identity/双评审）、review_kind 可互换（属性位 const 钉死）；SHOULD：plan critical 描述口径同步。全部核实采纳修复，按 §2.2 不再送第 3 轮
- 未采纳意见及理由：无。

## 验证
- 命令：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`
- 关键输出：
  ```
  74 passed
  ```
- 另：输出信封真机探针（codex exec 一次调用，EXIT=0、返回可解析单 json 块）——CP1.3 Runner 协议已derisk。
- 结论：通过（步①未收尾，步级验证待 CP1.4）。

## 遗留 / 回退
- 待办→CP1.3：接口桩六模块（草稿已在 scratchpad cp13/：runner/statestore/schemas/gate/goalbrief/compiler）+ 单测。
- 已知测试盲区（内审指出，记档）：test_skills 锚词校验不保证「skill 指示字段 ⊆ schema 必填」的完整性——该保证靠内审/外审人工核 + CP1.4 端到端真跑兜底。
- 回退：`git revert 9ee4c45`。
