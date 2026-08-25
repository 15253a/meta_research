# Bundle Stage Owner 操作

在读取正式状态、提出 Target、claim／wake／cancel Target 根 Session、恢复执行、
提交 TargetCompletionHandoff 或形成 Bundle disposition 前使用本文件。以下名称表达
语义调用，不冻结生产函数名、issuer、transport 或 lifecycle。

## 目录

- [权限](#权限)
- [Bundle 与 Target identity](#bundle-与-target-identity)
- [Target daemon 操作](#target-daemon-操作)
- [Target 根 Session 操作](#target-根-session-操作)
- [TargetCompletionHandoff 接纳](#targetcompletionhandoff-接纳)
- [原生结果](#原生结果)
- [Web event forwarding](#web-event-forwarding)
- [Target 合同 seam](#target-合同-seam)
- [Bundle 与 Stage 收口](#bundle-与-stage-收口)

## 权限

- Advancement Engine：拥有 Bundle StageRunRequest、Foreground Epoch、
  BundleReport disposition、ExhaustionProposal 与 StageCommit。
- Research Graph：拥有 accepted FormalPlan、Target identity／spec／dependency／
  frontier、Baseline／Variant／Evaluation identities、Formal Measurement、
  MetricResult、TargetCommit 与领域 currentness。
- Research Memory：拥有 immutable content、AssetVersion／MemoryRef、custody、
  integrity、availability 与 asset receipt。
- Agent Runtime：拥有 TargetRun、root Session binding、single-flight claim、
  cancellation／reconciliation lineage 与 execution receipt。
- Bundle 主 Agent：拥有 Session-local rolling strategy、Target candidates、
  跨 Target priority／dependency／parallel suggestions 与 Bundle disposition
  candidate。
- Target 根 Agent Session：独占一个 Target 的 workspace、实现、训练、结果驱动
  修改、checkpoint／terminal candidate 选择与 TargetCompletionHandoff。
- Target daemon：只执行 claim、wake、single-flight、cancel、reconcile 与 event
  forwarding。
- Agent Harness：执行根 Session 的原生工具循环、子智能体拓扑和事件流。

探索与审阅子智能体只接收分支所需的冻结输入，返回 candidate、finding 与
provenance。它们不取得 Target identity、workspace 最终写权、训练启动权、
handoff freeze 权或 Owner acceptance 权。

Research Graph 在 Target 开始前可以接受 Target identity／spec／dependency。Target
根循环新产生的 implementation、checkpoint、result、measurement 与 commit facts
只有在 TargetCompletionHandoff 被 Harness 验证后才能进入 RM／RG 接纳链。

## Bundle 与 Target identity

Bundle 在提出 Target 前使用下列只读或 proposal 操作：

    observe_bundle_stage_run(...)
    observe_bundle_run_binding(...)
    verify_delivered_context_pack(...)
    read_formal_plan(...)
    verify_reuse_inputs(...)
    propose_targets(rolling_strategy_slice, coverage, dependency_candidates)
    read_target_frontier(...)

read_formal_plan 必须返回 FormalPlanRef、canonical content hash 与直接绑定该 hash
的 current RG receipt。propose_targets 只提交 candidate；Research Graph 决定
Target identity、canonical spec、dependency 和 frontier。返回 TargetRef 后，Bundle
必须重新读取 spec content binding、receipt 与 current frontier。

Target spec 冻结 Target 执行前已经接受的语义和输入引用。它不冻结尚未产生的
implementation bytes、checkpoint、result 或 Metric，也不授权 RM／RG 在 Target
根循环中接纳这些生成内容。

## Target daemon 操作

Target daemon 对外只暴露以下机械语义：

    claim_target_root_session(
      target_ref,
      current_spec_binding,
      accepted_inputs,
      authorization,
      harness_binding,
      idempotency_key
    )

    wake_target_root_session(target_ref, wake_ref)
    wake_bundle(bundle_run_ref, wake_ref)
    cancel_target_root_session(target_ref, confirmed_cancel_ref)
    reconcile_target_root_session(target_ref, claim_ref)
    forward_target_events(target_ref, after_event_id)

### claim

claim 在创建或恢复根 Session 前原子验证 Target current／ready、spec binding、
accepted inputs、authorization 与 Harness availability。相同 idempotency key 和
payload 返回同一 claim；同一 Target 已有另一个 current 根 Session 时 fail closed。
claim 只授予根 Session 资格，不批准某个 implementation、命令、checkpoint 或结果。

### wake

wake 是 durable edge-trigger。payload 只含 subject、wake identity 与 reason code；
它不携带 Target 结果、stdout、Metric、workspace snapshot 或 Owner receipt。接收者
醒来后重新读取权威状态。重复 wake 可 coalesce，但不能跨 subject 改绑。

### single-flight

Agent Runtime 为每个 Target 保持至多一个 current 根 Session。旧 Session 只有在
明确 retired／cancelled／irrecoverable 并失去 current 资格后，后继 Session 才能
claim。Bundle、daemon restart 与并发请求都不能绕过该约束。

### cancel

cancel 只接受已确认且绑定 exact Target／claim 的请求。daemon 把取消交给 Harness，
等待机械 acknowledgement，阻止 cancelled identity 的后续 current effect，并保存
迟到事件为 historical observation。daemon 不把取消解释为科学失败、replan 或
TargetCommit。

### reconcile

reconcile 处理 lost ACK、daemon restart、Harness reconnect 与 handoff response
loss。它优先恢复同一 claim、root Session、workspace checkpoint 与 handoff
identity；已经冻结的 handoff 只查询原 identity，不重新从 live workspace capture。
外部效果仍不明确时返回 outcome_unknown，不盲目重放。

### event forwarding

forward_target_events 只转发带 exact Target／TargetRun／root Session identity 的
stdout、stderr 与工具 lifecycle event。它可以提供 bounded cursor 和重连，但不
解析科研含义、不生成 Metric、不触发 RM／RG 接纳，也不改变 root Session 状态。

## Target 根 Session 操作

Target 根 Session 直接使用 Harness 已授权的原生能力：

    read accepted inputs
    write Target workspace
    implement or reuse
    self-check
    train or evaluate
    inspect stdout, files, checkpoints and results
    revise and repeat
    spawn advisory children
    optionally spawn one focused long-run/log observer
    return closed TargetCompletionHandoff

这些是一个根 Session 内的连续闭环，不是 daemon 的一组阶段。根 Session 可以根据
真实结果反复修改实现和训练路线；中间内容留在 Target-local durable workspace，
不调用 RM content acceptance 或 RG measurement／role acceptance。

子智能体不能直接修改最终 workspace 或启动独立 Target 循环。根 Session 可以采纳
其 patch／finding，但必须亲自形成当前实现、运行结果与最终 disposition。
长训练观察子智能体只 tail 进程与日志并回报；它不能停止训练、选择 checkpoint、
修改实现或提交 completion handoff。

如果输出被用于自适应选择后续训练、checkpoint、参数或路线，根 Session 在最终
result document 中如实说明。正式 ProtocolVersion 与 measurement identity 由 RG 在
交接后从 accepted Target authority 派生。

## TargetCompletionHandoff 接纳

根 Session 对账所有在途命令和未知效果后，只返回一个闭合值：

    TargetCompletionHandoff {
      schema_ref,
      target_ref,
      target_run_ref,
      status: "completed",
      artifacts: [{role, relative_path}, ...],
      result_document_path,
      summary
    }

Agent 不提交 content hash、receipt、正式测量身份或 TargetCommit。Harness 证明该值
来自 current root 的 terminal evidence，轻 daemon 只查询和重放：

    run_or_resume_target_root(...)
    query_target_root_completion_evidence(...)
    finalize(handle, evidence)

finalize 内部按序让 RM 扫描并接受所选 bytes，再让 RG 使用 accepted Target spec、
FormalPlan、冻结输入、RM manifest 与 result document 派生正式测量和 TargetCommit，
最后由 AR 发布 Bundle inbox。任何一步 lost ACK 都查询原 evidence/ref 并幂等重放；
不能从 live stdout 推断 formal result。

## 原生结果

| Result | 调用者动作 |
| --- | --- |
| accepted | 保存 exact ref 与 receipt；重读 current frontier／coverage。 |
| rejected | 保存 feedback／receipt；wake 根 Session 形成 successor handoff，或升级 Bundle。 |
| stale | 重验 request、Target spec、inputs、claim 与 handoff；不替换成 latest。 |
| needs_input | 绑定 exact HumanRequest；只有 authoritative satisfied disposition 后 wake。 |
| outcome_unknown | reconcile 原 identity；明确结果前不重放写入。 |
| technical_blocker | 保存 exact scope；可恢复则 wake 根 Session，否则 Bundle 保持 blocked。 |
| idempotency_conflict | 停止写入并保存冲突 evidence。 |
| already_accepted | 验证原 payload／hash 完全一致后消费原 ref 与 receipt。 |

永久分离 root execution、TargetCompletionHandoff、RM asset acceptance、RG Formal
Measurement、MetricResult、TargetCommit、BundleReport 与 StageCommit。响应丢失时
总是查询原 operation identity。

## Web event forwarding

authenticated Web 只通过 daemon event forwarding 观察 Target 根 Session。投影可以
显示 current／historical identity、bounded stdout／stderr tail、工具状态与连接状态。

Web 与 daemon 均不得从以下内容创建或推断正式 Metric：

- stdout 文本或进度条；
- 日志中的 accuracy、loss、p-value、completed 等标签；
- 临时 JSON、checkpoint 文件名或工具退出码；
- event 到达顺序、缺口、重复或重连状态。

正式 Metric 只来自 TargetCompletionHandoff 选定、由 RM 接受的 result document，
经已接受测量合同与 RG validation 后成为 MetricResult。Web 可以同时
展示 live observation 与 accepted Metric，但必须把两种来源和 authority 明确分开。

## Target 合同 seam

生产实现必须提供下列行为；名称不规定最终签名：

    propose_targets(...)
    read_target_frontier(...)
    claim_target_root_session(...)
    wake_target_root_session(...)
    cancel_target_root_session(...)
    reconcile_target_root_session(...)
    forward_target_events(...)
    run_or_resume_target_root(...)
    query_target_root_completion_evidence(...)
    finalize_target_root_completion(...)
    read_target_commit(...)

完整 seam 必须证明：

- Target／spec／dependency 由 RG 接受后才能 claim；
- 每个 Target 至多一个 current 根 Session；
- daemon 的职责保持在六项机械操作内；
- 根 Session 独占实现、训练、结果驱动修改和 completion handoff；
- generated RM／RG facts 只在 TargetCompletionHandoff 后创建；
- lost ACK／restart 重用原 claim、Session 与 handoff identity；
- stdout Web projection 从不成为 Metric authority；
- handoff、Owner receipts、TargetCommit 与 StageCommit 保持独立。

缺少任一行为时返回明确 unavailable／blocked seam，不能用 workspace 文件、
daemon 内部表、Web event 或 fixture identity 冒充。

## Bundle 与 Stage 收口

Bundle 在 wake 或 restart 后重读 current Target frontier、accepted TargetCommits 与
Owner receipts。它不读取 Target workspace，也不从 stdout 重建结果。

Bundle 主 Agent 形成 deeply immutable BundleReport candidate，至少包含：

- exact StageRunRequest、FormalPlan 与 ContextPack；
- 每个 ExperimentKey 的 required cells 与 accepted TargetCommit；
- 每个 accepted TargetCommit 对应的 TargetCompletionHandoff evidence 与
  RM／RG／AR receipts；
- remaining routes、technical blockers、SemanticBarriers 与 unknown effects；
- realized、replan_required 或 ExhaustionProposal 的完整依据。

BundleReport 由获授权 Owner 验证和接受。只有 Advancement Engine 可以在 current
request、accepted BundleReport 与所有 required receipts 可验证时形成 StageCommit。
Target daemon 不构建 BundleReport、不选择 disposition，也不推进 Stage。
