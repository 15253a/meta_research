# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-07 14:25 ｜ 位置：步①（M0）CP1.1 契约层
- 检查点状态：外审第 2 轮上限、反馈已全部修复（53/53）→ 落提交中

## 正在做什么
CP1.1（契约层）收尾：内审 With fixes + codex 两轮 REQUEST_CHANGES 的全部 BLOCKER/SHOULD 已核实并修复（import_defer 焊死、fake↔synthetic 双向联动、evidence oneOf 互斥、claim 按 target_kind 必填、complete⇔success 绑定、skipped 免执行事实、requirements-dev.txt）。按 CLAUDE.md §2.2 不再送第 3 轮，直接落检查点提交。

## 工作区状态
- 未提交（即将 commit）：meta-research/ 全部新文件、ROADMAP.md、.gitignore、本文件。
- CP1.2 草稿（system_prompt + 4 skill + idea_audit/plan_review schema）已备在 scratchpad `cp12/`。

## 下一步动作（按序，具体到命令/文件）
1. `git add` 全量 → commit（消息带 CP1.1 + review/verify 行）
2. build_log/0006（草稿在 scratchpad/build_log_0006_draft.md，填 hash 与第 2 轮结论）+ INDEX + 勾 ROADMAP + 刷新本文件 → 记账 commit
3. 开工 CP1.2：scratchpad cp12/ 五份移入 prompts/，加 idea_audit/plan_review 两 schema + 更新清单测试与正负例

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示 2026-07-07，见 memory）。
- bundle_target/execution_* 词表以附录 A DDL 为准（log_kind 六值 train/eval/smoke/stderr/platform/import_clone；loss_trend down/flat/up/nan/unknown；oom_count；fold ⇒ ckpt_key；attempt failure_kind 八值）——别按第二部分 §6.11 的缩写自造。
- 两处待办给后续检查点：①goal_brief 解析规则现在 tests/test_schemas.py 里，CP1.3 移入 orchestrator 后测试反向 import；②规范内部缺口：《二》§6.12 提到 import 旋钮（selection_key 排序/policy_hash/license scope）而附录 C 无对应键——M1 开工前连同 OPEN #1/#2 一并问用户。
- M1 另一疑点：DB evaluation.source 枚举无 'fake'，M1–M3 假执行如何入账需 M1 确认（bundle_target schema description 已注明）。
- pip 装包走镜像源须不带代理；codex 网络须代理 127.0.0.1:7890（相反，勿混）。
