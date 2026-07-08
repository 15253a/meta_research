# 0033 · CP7.5 M6 长跑步级验证（收尾 M6 建造，test-only）

- date: 2026-07-08
- commit: f45dd6f — test: CP7.5 M6 长跑步级验证（§7.1 M6）
- branch: main
- 检查点 / 步: CP7.5（属：步⑦ M6 长跑 + 验收剧本 —— 本检查点收尾 M6 建造）

## 决策
§7.1 M6「数百轮无人值守不漂移」步级验证。**诚实裁量**：pure reasoning-only 循环受 policy.tree_guard
约束（max_decompose_depth=4/max_children_per_node=6/max_open_questions=30——decompose 使父被子依赖锁住、
只叶可调度且加深）→ 守卫下有限深度树，mock 广度填至 ~29 问题/~15 轮即 terminate。「数百轮」需 attack 轮
答问+前沿更新（阻于 plan 契约缺口，ROADMAP 载）。故本套验**尺度不变**三性质：

1. **不漂移**（守卫上界内 ~15 轮）：question_dep 引用完整 / 父指针有效（无孤儿）/ 全 cycle done / 终轮
   terminate / 恰一根 / ≥20 问题。（goal_ver 恒=1 是 reasoning-only 结构必然[无 goal_amend 路由]，不作
   漂移断言——换 question_dep 引用完整这一真结构检查。）
2. **可恢复**（durable 交接）：整段跑 vs **只跑 6 轮（在途未 terminate）后换新实例续跑到 done** → 终库
   （问题树+cycle 序）逐字节一致。首段在途 + 续段真推进（len>0）双断言防「跑完再空 resume」空真。
3. **τ 自停**：永不 terminate + 低分（两子都评分，否则 CP1 保守面「前沿有 NULL 分即非衰退」永重置）→
   τ 判据① 3 轮自停（last_stop_reason=score_floor，非跑满 500）+ 重启拒推进。

provider DB 确定性派生（非进程内计数）→ 换实例续跑产同一树（可恢复前提）。

## 改动文件
- `meta-research/tests/test_m6_longrun.py` — 新增：3 测（不漂移/可恢复/τ 自停）+ _bounded_provider（守卫
  安全广度 decompose，递归深度 CTE 选可分解浅叶）。
- `implement_note.md` — 记账。

## Review
- 内审（Opus 子代理）：REQUEST_CHANGES → 全修：①[BLOCKER] 可恢复空真——首段 max_cycles=30 > 全程 15 会
  跑完、resume 空转（0 轮），终库匹配只因首段独建整树、restart 边界从未落在途 → 改首段 6 轮（<15、在途
  未 terminate）+ 断言首段未收口 + 续段 len>0；②[SHOULD] goal_ver=1 结构空真（无 goal_amend 路由恒 1）→
  换 question_dep 引用完整；③[SHOULD] 尺度不变对 no-drift 略 oversell → docstring 明「不漂移只覆盖
  结构/投影漂移模式（依赖引用/父指针/投影每轮归零），表增长类需真长跑（阻于 plan 契约）」。其余
  （provider 确定性无隐藏态、τ 反事实[只评一子则永不停撞 max_open]、无 flaky、其余断言真验）逐项实证通过。
- codex（gpt-5.5/xhigh）第1轮：REQUEST_CHANGES——1 BLOCKER + 2 SHOULD + 1 NIT，全部采纳修复：
  ①[BLOCKER] τ 重启复用同一 state/StopController 实例（停机若内存缓存也过）→ 全新实例（重开 DB+新
  daemon/state/compiler/StopController）+ cycle 数未增断言；②[SHOULD] _tree_state cycle 序漏 question_id
  → 纳入 active_question_id/next_question_id + question_dep 投影；③[SHOULD] docstring goal_ver 不漂移
  措辞不一致 → 改依赖引用/父指针/cycle 终态；④[NIT] pytest 未用删 + §policy.tree_guard 命名。
- codex 第2轮：**APPROVE**（无发现——τ 全新实例重启+cycle 未增、restart-equivalence 纳入 active/next question+question_dep 投影、docstring/命名清理均确认闭合）。
- 未采纳意见及理由：无。

## 验证
- 命令：`python -m pytest tests/test_m6_longrun.py -q` → **3 passed**；`python -m pytest tests/ -q` →
  **535 passed**（CP7.4 后基线 532 + 3 新，无回归）。
- **步级验证（§7.1 M6 行）**：不漂移（结构/投影模式，守卫上界内）+ 可恢复（中途重启终库一致）+ τ 自停
  （价值衰退安全网）均过。**建造/执行边界**：数百轮真长跑 + §7.4 T1/T2 = 运维执行（真 Codex + 真 EEG，
  且 real-Codex attack 前须裁 plan 契约缺口）；本套验尺度不变性质，非字面数百轮。
- 结论：通过（**M6 建造面收尾**：自终止安全网 + 真 Codex 装配入口 + reasoning-only 全自动闭环 + §7.3
  机制验收 + 长跑步级验证全落；系统「完整运行、进入全自动」达成）。

## 遗留 / 回退
- 运维执行交付（本 session 外）：§7.4 T1/T2 真跑（数百轮×24h 真 EEG）。**real-Codex attack 前置**（须先
  裁 plan 契约缺口方向）：judge provider + idea/plan 消费者↔schema 校准 + stage-sidecar→create_file_request 桥。
- 回退：`git revert f45dd6f`（test-only）。
