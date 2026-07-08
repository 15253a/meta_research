# 0021 · CP5.2 PoolGate 注册/评审 gates + subject manifest

- date: 2026-07-08
- commit: 439d716 — feat: CP5.2 PoolGate 注册/评审 gates + subject manifest 确定性构造（M4）
- branch: main
- 检查点 / 步: CP5.2（属：步⑤ M4 真执行 + 真 log + import 物化）

## 决策
落地 §4.1.4 池注册家族**注册/评审侧**：PoolGate（继承 ExecGate，共享 _reject 审计/受限读/review_passed）+
subject manifest 确定性构造（§4.1.4 附注的 subject_hash 唯一定义）。要点：

- **gate_register_evaluation = §4.2.5(ii) 测量注册入口（单事务）**：结果评审通过后一次事务写 evaluation(create/
  append) + attempt(**success**) + metric_result[] + eval success/canonical——成功 attempt 此前**不存在**（执行期
  只有 staging；失败 attempt 才走 CP5.1 start/finish_attempt 入账）。对齐 §4.2.5「成功评估的 log/观测在(i)段
  不入库、成功 attempt 尚不存在」。
- **绑定核（本检查点最重要的评审收获）**：target↔variant 绑定三处（register_evaluation create/append 双侧 +
  _register_common_checks）且 **NULL 不作通配**（未绑=非法态）；register_baseline 须 **build** 目标、
  register_variant 须 **exec** 目标（expect_kind）。无此三核，可拿 variant A 的通过评审/target 把 variant B
  的测量注册入池（unsound registration——codex 两轮各抓一层：第1轮抓缺绑定核，第2轮抓 NULL 通配绕过）。
- **「入池」= legal 状态**（冻结 DDL 无独立池表；卡片/召回读 legal 池）。
- **smoke 判据结构性**：gate 不可读 execution_log（authorizer 拒）——由 build_target 已达 running（经 smoke
  阶段 + 代码评审）结构性保证。
- **I1 全口径**：metric_def 比较 name/direction/unit/compute_spec 四列（漏比会把不同单位/计算规格静默复用旧
  def，破坏 append-only 语义）。
- **§4.2.5(ii) 组合注**：register_evaluation 内部单事务；register_baseline/variant 是其后的池迁移短事务；把
  「注册段」整体裹一个事务的组合器 = CP5.4 attack advance（WriteDaemon 事务不可嵌套），docstring 已注。
- subject_manifest：canonical JSON（(kind,ref) 排序 + 键排序 + 紧凑分隔）→ sha256；code/result 两配方；
  result manifest 须 ≥1 checkpoint（checkpoint 是可评 target 本体，空集=换 checkpoint 不改 hash → 篡改不可见）。

## 改动文件
- `meta-research/orchestrator/gate_pool.py` — 新增：PoolGate（claim_baseline/claim_variant/register_evaluation/
  register_baseline/register_variant/new_protocol + _register_common_checks + _require_keys）。
- `meta-research/orchestrator/subject_manifest.py` — 新增：subject_hash + code_review_manifest + result_review_manifest。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图两行。
- `meta-research/tests/test_gate_pool.py` — 新增：20 测（manifest×3 / claim×2 / **build 全链**[claim→生命周期→
  双评审→register_evaluation→register_baseline→complete] / **exec 全链**[claim_variant→…→register_variant，
  仅 variant legal] / 绑定核×3[cross-variant、NULL 目标、错 kind] / append 模式[绑定拒+canonical 保留+abandoned 拒] /
  评审拒 / required·I2 / 非 factory / 跨 baseline / run_id 防御 / seq 撞干净拒 / I1×4[撞版/口径 direction/
  口径 unit/批内重复]）。

## Review
- **内审（Opus 子代理）**：APPROVE（无 BLOCKER；DDL 触发器不触面/单事务写序/继承关系实证核可）。4 SHOULD+5 NIT
  全修：① 干净拒契约统一（实证两处裸 IntegrityError：claim_variant seq 撞、new_protocol 批内重复 def）
  ② code_ref 语义（不塞 repro_cmd，加 code_ref/commit_hash 显式参——防污染喂卡片/召回的池资产字段）
  ③ run_id 防御（有 run_produced checkpoint 必须给 run_id，「run(success)?」的 ? 仅豁免非 run_produced 来源）
  ④ append 模式测试缺口 ⑤ result manifest 空 checkpoint 守卫 ⑥ 排序口径注 ⑦ purpose 白名单。
- **外审（codex-chatgpt gpt-5.5/xhigh）**：
  - 第1轮 REQUEST_CHANGES → 全采纳。**2 BLOCKER**：① 无 target↔variant/kind 绑定核（可拿 A 的评审把 B 入池；
    原 test_register_variant_only_moves_variant 还把 build 目标用作 variant 注册、把破坏固化进测试 → 重写为
    真 exec 全链）② I1 口径比较漏 unit/compute_spec。2 SHOULD（初变体态前置、缺键干净拒）+ 1 NIT（死 import）。
  - 第2轮 REQUEST_CHANGES（**2 轮上限**）：**1 BLOCKER**——绑定核用 `is not None` 把 NULL variant_id 当通配
    （NULL 目标过评审后可作任意 variant 的入池跳板）。**按 §2.2 凭反馈自行修复、不再送审**：三处绑定核改为
    严格相等（NULL=未绑非法，拒）+ register_evaluation 对 import 目标 NotImplementedError（CP5.5）+ 回归
    test_register_evaluation_rejects_null_variant_target。其余复核点（I1 全口径、exec 全链、缺键拒、初变体拒）
    第2轮均核可。
- 未采纳意见：无（两层三轮全采纳；第2轮 BLOCKER 依上限规则自行修复入本提交）。

## 验证
- 命令：`pytest tests/ -q`
- 关键输出：
  ```
  389 passed in 45.19s
  ```
- 结论：通过。（CP5.2 未收尾步⑤；M4 步级验证在 CP5.6。）

## 遗留 / 回退
- 待办：CP5.3 真执行管线 + 真 parser + policy observation 节（OPEN #5 落地）；CP5.4 attack advance 全链
  （含把注册段组合进一个事务）；CP5.5 import 物化（gate_start_build_target/register_evaluation 两处 import
  NotImplementedError 在此接）；CP5.6 语义判据收尾。
- 回退：`git revert 439d716`（两新模块+测试；未接 Advancer，回退不破基线绿）。
