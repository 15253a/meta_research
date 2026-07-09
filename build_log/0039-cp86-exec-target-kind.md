# 0039 · CP8.6 exec target kind——既有 legal baseline 上建变体

- date: 2026-07-09
- commit: a713509 — feat: CP8.6 exec target kind——既有 legal baseline 上建变体（M7）
- branch: main
- 检查点 / 步: CP8.6（属：步⑧ M7；正式可用性②之一）

## 决策
attack 从「只 build」扩到「build + exec」。exec = 既有 legal baseline 的新变体（消融/替换/超参——
真研究「建家族 + 变体迭代」的核心形态）。走正式 gate 通道（gate_claim_variant + gate_register_variant）。
**模型裁量收窄**（本检查点只做 exec）：eval target 在冻结 plan.schema 里对 create_evaluation 无 variant
引用（需设计）、import 是独立子系统（ImportWorker 需装配 + 自己的 judge subject）——归 CP8.6b，各自独立
检查点，避免一次搅乱冻结件。exec 已覆盖核心研究形态。

关键设计：
- exec 走 gate_claim_variant（**gate 自建 exec build_target，plan_ref=NULL**）与 build 的「终局统一 INSERT」
  异构 → _commit_plan_terminal 分支：build INSERT / exec UPDATE 补 plan_ref。
- 崩溃恢复（claim→terminal 窗口）：自占复用**严核未终局身份**（pending + plan_ref NULL + config 一致，
  derive 与 claim 两侧同口径）——防身份漂移把新 plan_ref 写到旧 config 的 variant 上。
- run.kind=target_kind（exec→'exec'，trg_run_target_consistent）；exec 只 register_variant（baseline 身份
  不动、不连坐）。
- gate append 绑定锚收准：build_target.evaluation_id==evaluation_id（显式格子声明；DDL-safe gate-policy）。

## 改动文件
- `meta-research/orchestrator/attack_stages.py` — exec 全链（derive 占坑判 + _is_own_exec_reoccupy 严核 +
  _claim_targets build/exec 分派 + 终局 build INSERT/exec UPDATE + bundle 孤儿清理 + run.kind/register 按
  kind 分派 + _check_manifest 允许 exec）。
- `meta-research/orchestrator/gate_pool.py` — gate_register_evaluation append 绑定锚 build_target.evaluation_id。
- `meta-research/prompts/skills/bundle/SKILL.md` — commands 段注 exec 同 build。
- `meta-research/tests/test_attack_advance.py` — +4（exec e2e / baseline_ref 非法拒 / kill-9 恢复 / config 漂移拒）。
- `meta-research/tests/test_gate_pool.py` — append 绑定测收准（同 variant 别 target 可追加 / 跨 variant 拒 /
  错格拒）。
- `meta-research/tests/test_run.py` — 拒因文本适配（exec 已受理，拒因改 legal baseline）。
- `ROADMAP.md` — CP8.6 勾选 + 切分声明（CP8.6b = eval/import/route）。

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内审（Opus 子代理）第1轮：REQUEST_CHANGES——1 BLOCKER（**实证复现**：exec kill-9 落在 gate_claim_variant
  与终局之间 → 旧版 exec 缺 build 有的「本轮自占放行」逃生口 → 重放无条件拒 → plan_ref=NULL 孤儿卡
  bundle 的 json.loads(None) 楔死）+ 1 SHOULD（exec 复用分支无测试）。全修：自占放行逃生口 + bundle NULL
  孤儿防御 + exec 崩溃恢复回归（终库与不杀逐字节一致）。
- codex 第1轮：REQUEST_CHANGES——2 BLOCKER + 2 SHOULD + 1 NIT，全部采纳修复：
  ①[BLOCKER] append 放宽过头（删创建者锚后同 variant 错格污染未堵）→ 绑定锚改 build_target.evaluation_id==
  evaluation_id（显式格子声明）+ 回归；②[BLOCKER] exec 自占放行未核持久化身份（config 漂移会把新 plan_ref
  写旧 variant）→ _is_own_exec_reoccupy 严核 pending+plan_ref NULL+config 一致，derive/claim 两侧同口径 +
  回归；③[SHOULD] NULL orphan 静默过滤 → 显式清理+decision；④[SHOULD] 补 config 漂移/错格回归；
  ⑤[NIT] ci_info→claim_info。
- codex 第2轮：**APPROVE**（首轮 5 项逐条确认已解决；append 改动未波及 create/register_*；orphan DELETE
  条件足够窄；config 漂移绕不过 derive）。附硬化建议「_claim_targets exec reuse 也独立重核 config」——已
  顺手采纳（复用分支自防，不只依赖 derive）。
- 未采纳意见及理由：无（全部采纳）。

## 验证
- 命令：`python -m pytest tests/ -q` → **623 passed**（基线 535 无回归；CP8.5 后 618 + 5 新）。
- exec 全链 e2e：预置 legal baseline → exec 变体（gate_claim_variant→manifest 真训练/评估→
  gate_register_variant 入池，baseline 身份不动、只本变体 legal）→ 真证据关问。
- exec kill-9 恢复：claim→terminal 窗口崩溃、稳定 work_root 续跑，终库与不杀逐字节一致 + 真完成。
- 结论：通过（build 家族 + 变体迭代核心研究形态覆盖；DDL 未动）。

## 遗留 / 回退
- 待办：CP8.6b（eval target[需设计 create_evaluation 的 variant 引用] + import_defer/ImportWorker 装配 +
  route dependency_wait 特化）；CP8.7（运维操作面文档 + 步⑧步级验证收口）。
- 回退：`git revert a713509`。
