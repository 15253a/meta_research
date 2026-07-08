# 0020 · CP5.1 ExecGate 执行生命周期 gates（池注册家族·执行侧）

- date: 2026-07-08
- commit: 7d64ec5 — feat: CP5.1 ExecGate 执行生命周期 gates（§4.1.4 池注册家族·执行侧，M4）
- branch: main
- 检查点 / 步: CP5.1（属：步⑤ M4 真执行 + 真 log + import 物化）

## 决策
M4 首检查点：落地 §4.1.4 池注册 gate 家族的**执行生命周期侧**九函数（M1 只做了 gate_close_question；
注册/评审侧 gate_claim/register_* = CP5.2）。同时把 **OPEN #5/#6 的全自动裁决**与 **M4 检查点计划（CP5.1–5.6）**
落 ROADMAP（决策内容，随本检查点外审）。

新模块 `gate_exec.py`（ExecGate），纪律同 SqliteGate：判据读走受限只读连接（authorizer 拒 9 表；执行判据只涉
允许集表，观测隔离由此焊死）；写走 WriteDaemon 单写短事务（长操作绝不持写事务——训练在事务外，gate 只入账）；
拒绝 = DECISION(actor=gate,type=reject) + GateReject；DB 触发器为最终焊死层。要点：
- **bundle 串行**：「当前目标」从 build_target 状态**结构性推导**（本 cycle 最小 seq 非终态者），不依赖内存
  cursor——崩溃重启即可从结构恢复（statestore 的 in-process bundle_cursor 只是提示）。
- **连坐**：failed **与 engineering_blocked** 均连坐（§4.1.4「失败/blocked 时」）且**仅 build/exec**
  （eval 目标也带 variant_id，无 kind 守卫会误伤被评变体；skipped 不连坐）。失败卡/通知 outbox = M5 基建。
- **review_passed（双评审机械判据，CP5.2 register 复用）**：json_extract 取该 target 最新 judge DECISION
  （新 FAIL 覆盖旧 PASS）+ payload.subject_hash == 编排器当下重算（产物变→旧 pass 自动失效）+
  runner_call(success/audit/对应 purpose)；json_valid 过滤（payload 无 DDL CHECK）+ **更新的畸形同类 DECISION
  fail-closed**（其 target 不可知，可能正是本 target 的新 FAIL——不让旧 PASS 静默生效）。
- **干净拒契约**：I2/scope-checkpoint 配对/required 覆盖等前置核 + 写事务 except IntegrityError→拒+审计
  （触发器兜底层拦下的畸形载荷不裸抛）。
- **check-then-act 注**：判据在写事务前读（单写者模型无并发窗口；M5 多写者须升级写锁内重跑，docstring 已注）。
- **import 目标 NotImplementedError**：生命周期随物化设计 = CP5.5（OPEN #6），不预写未审语义。

裁量（全自动，落 ROADMAP 步⑤，随本检查点外审）：
- **OPEN #5**：policy.yaml 补 observation 节（nan/divergence/loss_trend 阈值）随 CP5.3 真 parser 落地成文走评审；
  extraction_policy_hash = 该节规范化 JSON 的 sha256（P6 可回放）。
- **OPEN #6**：物化 worker cycle = route 终身 NULL（七研究形态不扩）+ 开轮事务内 DECISION(orchestrator,
  import_worker_cycle) 权威标记 + 收尾 mark_cycle_done 不产 cycle_report + 恢复时驱动循环识别 worker 标记交
  物化 resumer（CP5.5 落地时随 codex 复核）。

## 改动文件
- `meta-research/orchestrator/gate_exec.py` — 新增：ExecGate（gate_start/progress/finish_build_target、
  gate_start/finish_run、gate_start/finish_attempt、gate_finish/abandon_evaluation、review_passed、_reject）。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图加 gate_exec 行。
- `meta-research/tests/test_gate_exec.py` — 新增：28 测（生命周期链/串行/评审闸/subject_hash 漂移/run 链/
  attempt create·append/I2/required 覆盖/canonical 保留/连坐×2[failed+engineering_blocked]/eval 目标不动池×2/
  腐化态×3[多绑 eval/失同步 success attempt/重复登记]/畸形 payload fail-closed×2/import defer/拒绝审计）。
- `ROADMAP.md` — 修改：步⑤ OPEN #5/#6 裁决 + M4 检查点计划 CP5.1–5.6 + M3 移交清单。

## Review
- **内审（Opus 子代理）**：REQUEST_CHANGES → 全修。**1 BLOCKER**（实证复现）：engineering_blocked 不连坐会把
  池对象永卡 building（§4.1.4「失败/blocked 时」）。4 SHOULD：① 畸形 metric 载荷裸抛 IntegrityError 无审计
  → except 转干净拒 ② create 模式 complete 前置 fetchone 任取（evaluation.build_target_id 无 UNIQUE）→
  确定性判（全部回指皆 success）③ check-then-act 与 gate_close_question 强模式的分歧注释 ④ import 池连动
  是未审语义 → defer CP5.5。5 NIT（failed→running 重试复位、review_passed SQL 化、cycle 审计链、死变量、
  presence 判）全修。另实证核可：受限读连接 per-statement autocommit（WAL 见最新已提交）、_reject FK 安全、
  canonical 守卫、ROADMAP 裁决与 DDL 一致。
- **外审（codex-chatgpt gpt-5.5/xhigh）**：
  - 第1轮 REQUEST_CHANGES → 全修。**1 BLOCKER**：内审修法（`if var is not None`）过宽——eval 目标也带
    variant_id，连坐误伤被评变体 → 加 kind∈(build,exec) 守卫 + 回归。3 SHOULD：① finish_evaluation 显式查
    success attempt（防腐化态置 failed）② review_passed json_valid 防裸抛 + 更新畸形 fail-closed（防旧 PASS
    静默生效）③ docstring 允许读表集补 evidence。
  - 第2轮 **APPROVE**（零 BLOCKER/SHOULD）：连坐守卫、fail-closed 边界（row=None 时保守 False）、三判序
    均核可。2 可选 NIT（engineering_blocked 参数化、旧畸形不阻断恢复语义测试）亦已加。
- 未采纳意见：无（两层三轮全采纳）。

## 验证
- 命令：`pytest tests/ -q`
- 关键输出：
  ```
  369 passed in 36.53s
  ```
- 结论：通过。（CP5.1 未收尾步⑤；M4 步级验证在 CP5.6。）

## 遗留 / 回退
- 待办：CP5.2 注册/评审 gates（gate_claim/register_baseline·variant、gate_new_protocol、双评审 subject manifest
  确定性构造、pool 侧写）——review_passed 已备；CP5.3 真执行+真 parser+observation 节；CP5.4 attack advance
  全链；CP5.5 import 物化（含 import 目标生命周期，本 CP 的 NotImplementedError 处接）；CP5.6 语义判据收尾。
- 回退：`git revert 7d64ec5`（新模块+测试+ROADMAP 计划；未接 Advancer/driver，回退不破基线绿）。
