# 0035 · CP8.2 attack_stages 真契约化（消费冻结 schema + 正式 gate + manifest 驱动）

- date: 2026-07-09
- commit: d822add — feat: CP8.2 attack_stages 真契约化——消费冻结 schema + 正式 gate + manifest 驱动（M7）
- branch: main
- 检查点 / 步: CP8.2（属：步⑧ M7 plan 契约缺口补齐 → 全流程 real-Codex attack）

## 决策
把 attack_stages 从 toy TARGET_SPEC 捷径切到正式分层：plan 消费**冻结 plan.schema**（抽象 target），
执行命令改由 bundle 阶段的 execution_manifest（CP8.1）承载，plan 走正式 gate（gate_new_protocol I1 +
gate_claim_baseline I5），build_target.plan_ref 存 **resolved 切片**（冻结 target + 编排器派生的
protocol_id/protocol_ver/eval_key/target_set_hash），bundle manifest 交叉核此切片防旁路。**不解冻任何
冻结件**（plan.schema/DDL/MIGRATION 字节未变，test_frozen_contracts 锁证）。

关键设计：
- **可恢复短事务序列**（WriteDaemon 单写不可嵌套）：persist-then-consume plan.json（崩溃重放同 plan）→
  纯读派生（protocol/metric string→int 映射，身份=name，确定性）→ gate_new_protocol（幂等跳过）→ 逐目标
  gate_claim_baseline（本 cycle 已占则复用）→ 终局单事务落 build_target+required_metric+phase_commit+status。
- **全自动不楔死**：Codex 产的**任何**站不住的 plan（结构非法/未支持 kind/canonical_key 冲突/required 版本
  不符/I1）→ 一律 _PlanReject/GateReject → 业务拒（decision(plan_rejected)+零 target 终态）→ reasoning 收
  inconclusive，绝不 raise 到 advance。
- 保留 M4 bundle 的两段提交 + 全部结构恢复骨架（只换命令来源 spec[...]→manifest/slice）。

## 改动文件
- `meta-research/orchestrator/attack_stages.py` — 重写：_idea_stage（content_md 机械合成 + audit 派生）/
  _plan_stage（schema 闸 + 派生 + 正式 gate + 可恢复序列 + 不楔死）/ _derive_plan（protocol/metric int 映射 +
  canonical_key 唯一·占用 + required 版本核）/ _obtain_manifest（validate+cross_check+净土物化）/
  _drive_target·_run_and_register（manifest 驱动 smoke/train/eval + checkpoint_dest）/ subject hash 用切片+ledger。
- `meta-research/orchestrator/compiler_sqlite.py` — 修改：_bundle_target 锚区真切片渲染（切片全文 +
  plan_slice_hash + required int 绑定）。
- `meta-research/orchestrator/import_worker.py` — 修改：_metrics_from_eval_log 去 spec 参（共享 staticmethod）。
- `meta-research/tests/test_attack_advance.py` — 重写 fixtures（schema-conform idea/plan + bundle provider 产真
  toy 代码经 manifest 真执行）+ 全部 M4 恢复剧本保留 + 新增 plan 不楔死回归（5 例：required 未声明/结构非法/
  exec 未接/同轮重复 ck/required 版本不符）。
- `meta-research/tests/test_frozen_contracts.py` — 新增：plan.schema sha256 锁 + 语义锁（无执行字段）+
  MIGRATION_SHA256 锁。
- `meta-research/tests/test_m4_semantic_cases.py` — 适配（bundle body 覆盖 + 删未用 import）。

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内审（Opus 子代理）：REQUEST_CHANGES → 全修：①[SHOULD-高] _plan_stage 不校验 plan + 结构取键在 try 外→
  裸 KeyError 逃逸楔死；required 版本可与 metric_defs 不符、同轮重复 canonical_key 别名共 variant→均推迟到
  bundle 才 GateReject 楔死 → 加 _validate_plan_schema + 结构取键全裹 try（kind→_PlanReject）+ derive 加
  canonical 唯一/占用 + required 版本核；②[SHOULD] 多目标 claim 半途留孤儿毒化 key → derive 前置拦 +
  claim 段 GateReject 时 DELETE 孤儿；③[NIT] 删未用 import sys。核对确认：命令源迁移彻底（无残留 spec[cmd]）、
  恢复/subject hash/防旁路三处同 canon 口径、frozen 锁为真锁。
- codex 第1轮：REQUEST_CHANGES——3 BLOCKER + 2 SHOULD，全部采纳修复：
  ①[BLOCKER] 复用既有 protocol 只核 scope 不核 metric 绑定 → required 指向未登记 metric → bundle 才 I2 拒
  楔死 → _derive_plan 在 proto_exists 分支核 required 全 ∈ protocol_metric，否则 _PlanReject（+回归：预置
  toy-proto@1 只绑 acc、plan 要 f1 → 派生期拒零占坑）；
  ②[BLOCKER] target_key 未核唯一（claims dict 覆盖错绑）+ seq/required.target_key/metric_id 未核 →
  派生入口四项唯一性/引用完整性校验，违反 _PlanReject（各配参数化回归）；
  ③[BLOCKER] plan artifact 获取/解析在 try 外（缺 plan.json 键 / JSON 损坏 → 裸异常逃逸楔死）→
  _plan_artifact 内转 _PlanReject + 调用移入 try（plan=None 支持哨兵 hash）；
  ④[SHOULD] resume 路径未 re-validate manifest → _check_manifest 抽出，fresh/resume 同口径（失败分流：
  fresh=_BundleReject 业务拒；resume=数据损毁 ManifestError fail loud）；
  ⑤[SHOULD] bundle 契约违规楔死 → _BundleReject + _drive_target 外层捕获（详见第2轮收窄）。
- codex 第2轮（上限）：REQUEST_CHANGES——逐条核实后**一采纳二不采纳**（§2.2：第2轮后自行裁决、不再回送）：
  - [BLOCKER-B 采纳] _drive_target 过宽捕获 GateReject 会把状态机/损毁类拒绝误终态化 → 收窄：外层只捕
    _BundleReject；gate_register_evaluation 调用点显式 try/except GateReject→_BundleReject(protocol_violation)
    ——其余 gate 拒一律 fail loud。加正反两向回归（eval 缺 required → failed(protocol_violation)；状态机
    GateReject → 上抛不误终态）。
  - [BLOCKER-A 不采纳（误读）] 称 `pack.target_id` 会 AttributeError、593 不可能过——实情：SqliteCompiler.render
    返回 **ContextPack 结构体**（compiler_sqlite.py:65，target_id 是其字段；attack 传 str(bt_id)），非纯字符串；
    594 测真实全绿。评审者只见内联 diff、把锚区渲染函数返回 str 误当 render 返回值。
  - [SHOULD-C 不采纳（主路径已满足+剩余不适用）] 建议 reject 也先物化哨兵防「崩溃后重调 provider 由拒变成」
    ——实情：非法 plan.json **已经**先物化后校验（重放得同一拒绝，确定性）；剩余窗口（信封缺键/fresh manifest
    非法）与**一切** provider 调用固有的「产出→持久化之前」窗口等价（合法产物同样存在），确定性 provider 下
    恢复测试成立，非本检查点引入的新洞。
- 未采纳意见及理由：见上（BLOCKER-A 误读；SHOULD-C 部分不适用）。

## 验证
- 命令：`python -m pytest tests/test_attack_advance.py -q` → 26 passed；`python -m pytest tests/ -q` → 594 passed。
- 关键输出：
  ```
  26 passed in 14.10s
  594 passed in 90.92s (0:01:30)
  ```
  基线 535 + 59 新/改（attack 全链 manifest 驱动 e2e + M4 恢复剧本全保留 + plan/bundle 不楔死回归 11 例 +
  状态机 fail-loud 反面回归 + frozen 锁 3）。
- **步级验证（部分）**：真 attack 轮**端到端跑通**（idea→plan[真 gate 注册协议/占坑]→bundle[manifest→
  harness 真子进程 smoke/train/eval]→注册入池→真证据关问→terminate），mock provider 驱动真组件。真 Codex
  CLI 冒烟 = CP8.4。
- 结论：通过（冻结件字节未动；全链真契约跑通）。

## 遗留 / 回退
- 待办：CP8.3 生产装配（bundle SKILL.md 真执行契约 + judge provider + StageProvider bundle/judge 扩展）；
  CP8.4 run.py attack 全装配 + 真 Codex 冒烟；CP8.5 sidecar 桥；CP8.6 exec/eval kind + import_defer。
- **诚实边界**：多目标 build plan 跨 resume 半途失败的孤儿清理只覆盖单调用（fresh）——多目标 build 非当前
  交付面（单 build target 已验），完整跨 resume 事务补偿 = CP8.6 硬化。
- 回退：`git revert <HASH>`。
