# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-07 14:35 ｜ 位置：步①（M0）CP1.2 流程层
- 检查点状态：开工（CP1.1 已完成：commit 965d1a3 + build_log 0006）

## 正在做什么
CP1.2（流程层）：prompts/system_prompt.md + 四阶段 SKILL.md（全中文；idea 两 runner_call：生成+独立判官 / plan 复用判定+可回答性评审独立调用 ≤2 轮 / bundle M0 驱动器代跑+双评审占位 / reasoning R1–R4 与 reasoning-only 轮）+ 两份过程产物 schema（idea_audit：$ref 复用 idea_set 的 audit_score；plan_review）。**五份草稿已写好**在 scratchpad `cp12/`（system_prompt / idea_SKILL / plan_SKILL / bundle_SKILL / reasoning_SKILL + 两 schema），移入仓库后：更新 tests 清单断言（STAGE_SCHEMAS 增两项）+ 补两 schema 正负例 + skill 实读校验测试（要素齐全性）。

## 工作区状态
- 干净（CP1.1 与记账均已提交）。
- 产物信封约定（CP1.2/1.3 共用）：codex 最终回复 = 单个 ```json 块 `{"files": {...}, "md": "..."}`；本机 bwrap 不可用 → prompt 声明全部输入已内联、无需执行命令。
- idea_audit.schema 用跨文件 $ref（https://meta-research.local/schemas/idea_set.schema.json#/$defs/audit_score）→ 校验器需 referencing.Registry 装载全部 schema（conftest 要加 registry helper）。

## 下一步动作（按序，具体到命令/文件）
1. `cp scratchpad/cp12/* → meta-research/prompts/{system_prompt.md,skills/<stage>/SKILL.md} + schemas/`（路径映射：idea_SKILL.md→skills/idea/SKILL.md 等）
2. tests：conftest 加 schema Registry；test_schemas STAGE_SCHEMAS + fixtures（idea_audit 正/负、plan_review 正/负）；新增 test_skills.py（实读校验：每 SKILL 含 触发/读取/任务/产物 schema 指向/门禁写入/失败语义 六要素 + 关键锚词如 R1–R4/NEED/复用判定/两段提交）
3. 内审（Agent model:"opus"）→ 修复 → `git add` → `bin/codex-review.sh "CP1.2 流程层……"` ≤2 轮 → commit → build_log/0007
4. 然后 CP1.3（接口桩：gate/statestore/compiler/ctx/recall/runner + validate_artifact；goal_brief 解析移入 orchestrator）

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- bundle_target/execution_* 词表以附录 A DDL 为准（log_kind 六值；loss_trend down/flat/up/nan/unknown；oom_count；fold ⇒ ckpt_key；attempt failure_kind 八值；content_hash 非 hash）。
- 契约焊点（外审两轮的成果，改 schema 时勿松）：import_defer ⇔ targets=[] 且锚必填；fake ⇔ synthetic 双向；evidence oneOf 互斥；claim 按 target_kind 必填；complete ⇔ 嵌套全 success；skipped 无执行事实；负例必须配 .expect。
- 留给 M1 开工前问用户：①《二》§6.12 提及 import 旋钮而附录 C 无键；②DB evaluation.source 无 'fake'，M1–M3 假执行入账方式；③OPEN #1/#2。
- pip 走镜像源不带代理；codex 要代理 127.0.0.1:7890。测试跑法：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`。
