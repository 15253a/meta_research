# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-07 15:10 ｜ 位置：步①（M0）CP1.3 资产层接口桩
- 检查点状态：开工（CP1.1 965d1a3 / CP1.2 9ee4c45 已完成，build_log 0006/0007）

## 正在做什么
CP1.3：orchestrator 六模块——schemas.py（SchemaSet+ARTIFACT_SCHEMA_MAP）/ gate.py（StubGate：schema+引用两级真、业务放过；ArtifactIndex；staging 原子写）/ statestore.py（InMemoryStateStore：七 op、调度可见性、close_question/release）/ compiler.py（StubCompiler 四区包+manifest 溯源+确定性；StubCtx/StubRecall）/ runner.py（CodexRunner：codex exec ephemeral、信封解析、transcripts 归档）/ goalbrief.py（启动契约解析，tests 反向 import）。**六份草稿已在 scratchpad `cp13/`**，移入后写单测（gate 拒非法/statestore op 语义/compiler 确定性/runner 用假 bin 测解析、不烧真 token）。

## 工作区状态
- 干净（CP1.2 与记账均已提交）。
- scratchpad `cp13/` 草稿注意两处需同步终稿：schemas.py 的 idea_set_draft 已有真 schema 文件（不再用 _draft_validator 派生——删掉派生逻辑、直接映射 "idea_set.draft.json"→"idea_set_draft"）；statestore.create_root 支持 local_key（tree_ops schema 已加）。

## 下一步动作（按序，具体到命令/文件）
1. cp scratchpad/cp13/*.py → meta-research/orchestrator/（按上述两处修正）
2. tests/test_orchestrator.py：gate 两级校验（用既有 invalid fixtures 过 StubGate 应拒）+ statestore（bootstrap→attack→decompose→聚合解锁→terminate 链路 + 拒绝判据）+ compiler 确定性（同状态两次 render pack_hash 相等）+ runner 信封解析（METARESEARCH_CODEX_BIN 指向假脚本）+ goalbrief 迁移（test_schemas 的解析测试改 import orchestrator.goalbrief）
3. 内审（Agent model:"opus"）→ 修复 → git add → bin/codex-review.sh ≤2 轮 → commit → build_log/0008
4. CP1.4：driver.py（advance 环 + route 派生 + idea 两调用/plan 评审环/bundle 假执行）+ M0 验收脚本跑 toy 3–5 轮（真 codex 调用，模型/档位走 env）

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- RFAIL 口径已裁定并焊进 schema/skill：已执行失败恒 failed（携事实），仅未执行旁路 skipped（不携任何执行/失败/评审事实）；图 05 原文"非 critical→skipped"是措辞松动（build_log 0007 有记录）。
- bundle_target 契约焊点清单见 build_log 0006/0007（fake⇔synthetic 双向、complete⇒全 success+双评审 kind+pass、skipped 全禁、fold⇒ckpt_key、DDL 词表）。改 schema 勿松。
- 留给 M1 开工前问用户：①《二》§6.12 import 旋钮 vs 附录 C 缺键；②DB evaluation.source 无 'fake' 的 M1–M3 入账方式；③OPEN #1/#2。
- pip 镜像源不带代理 / codex 要代理；测试：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`。
