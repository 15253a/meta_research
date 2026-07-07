# SKILL · bundle —— 逐目标串行执行（KIND 分支 + 双评审）

> 版本：m0-1。按《第一部分》§3.4 与流程图 05-Bundle；产物 schema = `schemas/bundle_target.schema.json`
> （逐目标一份）。
> **M0 桩行为（重要）**：本阶段由驱动器确定性代跑——不起 Codex 构建会话、不真训练；
> 每个 target 由驱动器生成**造假产物**（evaluation.source='fake'、execution_log/observation
> synthetic=true），双评审为**占位判官**（review 对象 stub=true、自动 pass）。
> 本文件同时写明 M4 起的真执行契约：届时只换执行方式、不换产物契约与流程序。

## 通用

- **触发条件**：plan 产出 targets 非空（route=attack / eval_only）。
- **读取**（逐目标）：计划切片（该 target + 协议 + required_metric）+ identity 模板 + env 锁 +
  依赖基线 identity · 同 failure_kind 失败卡。
- **门禁与写入**：逐目标产 bundle_target 产物 → 三级校验 → gate 注册（M0 门禁桩放过业务级）。
- **失败语义**（P4 失败即观测，不自行兜底）：
  - 工程问题在阶段内自愈（smoke 修复循环无次数上限、受预算+watchdog）；预算尽+追加一次+
    换会话仍败 → `engineering_blocked` + 通知人，**工程失败永不入问题树**；
  - 训练失败/超时 → run(failed,failure_kind) + 失败卡 → 目标 failed；
  - 训练成功但评估失败 → run 保持 success + attempt(failed)，目标 failed（两条不同的边）；
  - 双评审轮数用尽 → 目标 failed(review_failed)（代码评审失败不训练省成本；结果评审失败
    测量整包不注册，run+checkpoint 保留）；
  - RFAIL 分流（状态归属对齐 06f 状态机）：**已执行而失败的目标恒为 `failed`（携
    failure_kind 与执行事实，成败同记）**；critical 失败 → **早退**带失败摘要，此时
    **剩余未执行的 pending 目标才置 `skipped`**（未执行旁路，不携任何执行事实）；
    非 critical 失败 → 本目标记 failed，继续取下一个 pending 目标；
  - 一切失败裁决权属轮尾 reasoning。

## 执行流程（每 target，严格串行；游标 bundle_cursor 由编排器落库）

按 `target_kind` 分支：

- **eval（免训练）**：用既有 legal 变体 checkpoint 评估 → 结果落 staging →
  **结果评审**（result_review）→ 三级校验 → gate_register_evaluation（按 eval_action 定位/
  创建 evaluation 或追加 attempt）。**不建 run**。评估失败/超时 → attempt(failed)。
- **build / exec**：开隔离工作区（M4：Git worktree，写权仅 worktree+artifacts、网络默认关）→
  构建变体（config + overrides，不复制整份 src）→ **smoke**（快速能跑判定）→
  **代码适配评审**（code_review：独立评审只见 plan 切片+代码产物+smoke 结论；判据 = 实现与
  plan 适配、无缩水、无不当实现；≤policy bundle_code_review 轮）→ 训练 run(kind=build|exec) →
  checkpoint(s) → **出厂评估**（source=factory）→ **结果评审**（result_review：对象一律 staging
  产物——结果 artifact + checkpoint hash + 运行日志/观测 staging + identity 草稿；判据 = 结果与
  log 合理、据结果反查代码无明显 bug；**log 仅供评审读、不进门禁不作证据**；
  ≤policy bundle_result_review 轮）→ 三级校验（I2）→ 注册入池（复制非剪切）→ 目标 complete。

**两段提交纪律**（§4.2.5；M0 由驱动器模拟同一顺序）：段(i) 执行事实**随发生**短事务入账
（start_run 开训即 running、finish_run+checkpoint、run-owned log/观测、失败 attempt）；
段(ii) **结果评审通过后**才单事务注册测量整包（evaluation+attempt(success)+metric_result+
attempt-owned log 补登+gate_register_*+target complete）。评审否决 → 段(ii) 不发生
（run(success)+checkpoint 保留、测量不注册）。目标间不共事务，崩溃从 bundle_cursor 续。

每 target 产物必含：execution_logs（train/eval/smoke 的 ref+content_hash）+ execution_observation
（结构化观测）+ identity 草稿（build/exec）。**M0：以上全部由驱动器造假生成并显式标记**
（source='fake'、synthetic=true），metric 数值为固定 toy 序列——只验流程契约、不代表任何研究结论。
