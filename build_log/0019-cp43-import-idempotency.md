# 0019 · CP4.3 select_deferred 幂等守卫（不重复登记）——收尾 M3

- date: 2026-07-08
- commit: 6dd2387 — feat: CP4.3 select_deferred 幂等守卫——「不重复登记」fail-loud（收尾 M3）
- branch: main
- 检查点 / 步: CP4.3（属：步④ M3 编排器 Advancer + 恢复 + import 降级）——**M3 收尾检查点**

## 决策
M3 §7.1 import 隔离验收 = 「pending question_dep 排除调度 + **不重复登记** + 不产 target」。除「不重复登记」外均已由
既有 test_isolation_m1c 覆盖（CP2.4）；缺口在：external_import append-only、DDL 无 selection_key 唯一约束 →
select_deferred 重放会重复三写入（重复占位 baseline / import / dep）。

本检查点在 select_deferred 事务内加**幂等守卫**：同 (question_id, selection_key) 已有 selected_for_materialization →
返回既有三元、不重复三写入。**幂等只豁免真重放，其余 fail loud**：
- candidate_id 不符 → ValueError（选择锚错乱，拒绝静默复用旧登记冒充成功）；
- license_review_id 不符 → ValueError（授权前置条件是契约、非「首次登记为准」的审计细节。**同值比对**而非重验 allow：
  幂等语义锚定「同一次授权裁定」的契约身份，不绑可变当前态；重验反会静默吞掉 append-only 违规）；
- 同锚多条登记（fetchall len>1）→ ValueError（无 DB 唯一约束阶段主动探测腐化态，勿 fetchone 任取）；
- question_dep 缺失 → ValueError「隔离锚破损」（dep 是不产 target/不可调度的关键隔离锚；不以 None 伪装成功）。
candidate_set_hash/policy_hash 等审计锚分量按首次登记为准（同候选 + 同裁定已证同一选择）。

**锚假设（M4 接手者，注释已入代码）**：(question, selection_key) 唯一标识**在生效**选择——M3 成立因 ① select 后问题带
pending dep 不可调度（调度器不可能再出新选择）② M3 无 supersession（=M4；届时须改判「未被 superseded 的」并复核
selection_key 每选择唯一）。check-then-act 依赖单写者（WriteDaemon 单连接 + BEGIN IMMEDIATE）；M5 多写者须补 DB 约束。

**范围诚实声明（内审 SHOULD 要求言明）**：本守卫 = **importer 函数级幂等**（对 select_deferred 直接重调的防重）。
架构上更强的 **phase_commit 级幂等**（import 三写入与 set_route(dependency_wait) / Qn 释放 / mark_cycle_done 捆同一
phase_commit，键 (cycle,stage,target)+artifact_hash，§4.2.5/§3.6.3 line579）随 Advancer 的 dependency_wait plan
阶段接入 = **M4**（需 plan 阶段真环，同 attack 轮）。M3 验收字面（不重复登记断言）由本守卫满足，不作过度声明。

## 改动文件
- `meta-research/orchestrator/importer.py` — 修改：select_deferred 事务内幂等守卫（重复探测 / candidate / license /
  dep 四道 fail-loud + 真重放返回既有三元）+ 锚假设与单写者依赖注释 + docstring 幂等条目。
- `meta-research/tests/test_isolation_m1c.py` — 新增 5 测：幂等重放（r1==r2 + external_import/baseline/question_dep
  计数均 1）/ candidate 不符 / license 不符 / dep 缺失（直删模拟破损）/ 重复登记腐化态（旁路直插第二条）。

## Review
- **内审（Opus 子代理）**：APPROVE（无 BLOCKER）。实测发现幂等路径绕过 candidate/license 校验的静默成功洞（2 次
  empirical probe）。3 SHOULD 全修：① fail-loud 候选校验 + 负测 ② 锚假设（selection_key ⊊ I6 七元锚）+ 单写者
  check-then-act 注释 ③ build_log 须言明函数级幂等、phase_commit 级=M4（即上「范围诚实声明」）。
- **外审（codex-chatgpt gpt-5.5/xhigh）**：
  - 第1轮 REQUEST_CHANGES（无 BLOCKER；2 SHOULD+1 NIT 全采纳）：① license_review_id 同值比对（授权前置条件非审计
    细节）② dep 缺失不得返回 None 伪装成功 + 同锚多条 fetchall 探测 ③ 补两条回归测试。
  - 第2轮 **APPROVE**（零 BLOCKER/SHOULD）：判序成立（重复探测→candidate→license→dep→真重放返回）；「license 同值
    比对而非重验 allow」取舍成立（幂等语义锚定同一次授权裁定的契约身份）。1 可选 NIT（重复登记腐化态测试）亦已加。
- 未采纳意见：无（内审 + 外审两轮全采纳）。

## 验证
- 命令：`pytest tests/ -q`
- 关键输出：
  ```
  341 passed in 20.66s
  ```

### 步级验证（本检查点收尾步④ M3）——跑 §7.1 M3「验证方法」
M3 两条可证伪判据逐条映射到测试（10 测全绿）：

- 命令：`pytest tests/test_advancer.py::test_kill9_recovery_final_state_identical tests/test_advancer.py::test_run_cycles_resume_after_restart tests/test_isolation_m1c.py::test_pending_baseline_dep_blocks_scheduling tests/test_isolation_m1c.py::test_select_deferred_idempotent_no_duplicate_registration tests/test_isolation_m1c.py::test_select_deferred_replay_mismatched_candidate_fails_loud tests/test_isolation_m1c.py::test_select_deferred_replay_mismatched_license_fails_loud tests/test_isolation_m1c.py::test_select_deferred_replay_missing_dep_fails_loud tests/test_isolation_m1c.py::test_select_deferred_duplicate_rows_detected_fails_loud tests/test_isolation_m1c.py::test_import_produces_no_executable_target_or_pool_entry tests/test_isolation_m1c.py::test_select_deferred_atomic_rollback`
- 关键输出：`10 passed`
- 逐条映射：
  1. **任意阶段 kill -9 重启续跑终库与不杀一致（排除 timestamps/attempt_id/log offset）**：
     `test_kill9_recovery_final_state_identical`（真 SIGKILL subprocess，decompose 阶段将写未写时杀 → 新实例续跑 →
     `_final_state` 确定性列全等）+ `test_run_cycles_resume_after_restart`（in-process 重启续跑同终库）→ 通过。
     范围注记（ROADMAP 步④裁量）：M3 恢复验收覆盖 reasoning-only 轮（bootstrap/decompose/terminate）；attack 轮
     idea/plan/bundle 阶段恢复依赖池注册 + 真执行 = M4 扩展。
  2. **import deferred 隔离：pending dep 排除调度、不重复登记、不产 target**：
     `test_pending_baseline_dep_blocks_scheduling`（§4.2.1 调度排除）+ `test_select_deferred_idempotent_no_duplicate_registration`
     （不重复登记，幂等）+ 4 条 fail-loud 契约测 + `test_import_produces_no_executable_target_or_pool_entry`
     （不产任何 target / 不入池 / 无物化 / 不写 decision）+ `test_select_deferred_atomic_rollback`（三写入原子）→ 通过。
- **结论：步④（M3）步级验证通过。** 全量 341 绿。

## 遗留 / 回退
- 待办（M4）：attack 轮（idea/plan/bundle 阶段 advance + phase_commit 幂等 + 恢复扩展）；池注册 gate_register_*
  （15 函数，build_log 0013）；真执行 + 真 log/观测 + parser_result_suspect 真派生（复用判定才可对真执行上线）；
  import 物化（materialize：占位 baseline→legal，scope 消费点校验；supersession——届时改判本守卫「未被 superseded 的」）；
  phase_commit 级 import-defer 捆绑（三写入+set_route+Qn 释放+mark_cycle_done 同一事务）。
- 回退：`git revert 6dd2387`（importer 幂等守卫 + 测试；无契约破坏，回退仅失去防重）。
