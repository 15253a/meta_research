# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-13 ｜ 位置：步⑪ CP11.4c.3d.2 目标生产环境 / 最终验收
- 检查点状态：代码级基本可用；CP11.4c.3d.1.3 功能 `c83e34f` 已提交，等待外部基础设施

## 上一检查点结果

- compiler 原先把合法 `mr1` 与 metric definition/version 拼成 `mr1:1@1=...`，模型合理地复制了
  `mr1:1@1`，而 Gate 只接受 exact `mrN`，导致已有成功测量却无法关题。
- 现在固定锚以 `successful_measurements=[evidence_ref=mrN; metric=...; value=...; scope=...]` 明示引用；
  reasoning 只复制 `mrN`。fake/real 不再按“当前/历史”猜，而只看该测量显式 `source/synthetic` provenance，
  因而 production 真执行不被降格，同时共享 M0Driver 的当前假执行仍诚实标 fake。
- 对旧 c3 四项真实 metric 的隔离 reasoning+Gate 探针输出 `metric_result_id=mr1`、正文不含 M0/fake，带
  parser-suspect 过滤的 Gate 成功写 `a1`；同原始 c3 snapshot 的 console query 经真实 Codex rc16 成功回复。
- compiler/skill/M0 driver 相关 **56 passed**；内部首审发现 M0 provenance Major，修毕后终审 APPROVE。
  依用户要求未跑仓库全量。

## 当前可用边界

- 默认 production 装配的 query/import adapter sideband 现在与 non-root preflight 契约兼容，且默认
  `/usr/local/bin/codex` 已有真实启动/应答证据，不再是“preflight 能启动、首次 query 必失败”。
- 单节点 development 已真实跑通 bootstrap、decompose、idea、plan/review、bundle、Docker execution 与双 review；
  后续隔离 replay 又证明 reasoning→Gate 关题；同一原始 snapshot 的 tool-free query sideband 也成功。
  修复没有增加第二 DB、daemon、scheduler、SSH orchestration 或通用 workflow。
- CP11.4c.3c 薄工具已闭合：qualification firewall、shared SQLite boundary、local/two-node
  canary 协议、fixed-linear fault runner 与 evidence pack 可组合使用。
- evidence v1 证明包内 bytes 闭包和一轮续跑，不是 restore engine，不声称完整 work-root DR、
  来源签名、真 Codex/GPU/two-node 或 qualification 已验收。
- 当前节点仍只有单机且无 NVIDIA container runtime；目标 GPFS 两节点正向、真实 ≥200 轮、
  T1/T2、故障注入组合验收和最终全量均未执行。

## 下一步动作

1. 进入 CP11.4c.3d.2：在 dedicated VM/private cgroup + NVIDIA Docker + 目标 GPFS quota/second
   node/connector 到位后，按既有 runbook 执行真实 ≥200 轮。
2. 把 two-node canary、预声明 fault schedule、干净 restore+续跑 evidence pack 与 T1/T2 qualification
   作为同一生产验收批次，保存 manifest hash 到外部不可变审计记录。
3. 只在上述最终验收提交前跑一次仓库全量；当前不再重复长验证。

## 关键坑

- `reference/` 原始是单机 embedded SQLite WAL；两节点 VEPFS 是后续生产加固，不得倒称原始要求。
- evidence 目录 hash 是内容身份而非来源签名；没有外部保存 hash 时，同 UID 重写整包不能靠自证发现。
- `real_codex_resume_verified` / `qualification_receipts_verified` / `full_restore_verified` 仍必须为 false；
  不得用注入式 worker 的一轮续跑替代生产验收。
- 当前 host 能看到 8×A100-80GB，但 Docker 没有 NVIDIA runtime/cgroup/resource limit。清理本轮旧临时目录后
  pinned CPU image 曾完成真实 build/run；本轮新三轮 smoke 又在 container create 遇 `/ebs` ENOSPC，说明当前
  20G 节点不具持续运行 headroom。pytest basetemp 必须放 VEPFS，生产长跑不得使用该节点。
- 隔离 reasoning+Gate 是对真实 c3 metric 的定向 replay，不是官方 restore/resume 或全新 attack 轮；它只证明
  本次引用修复越过原 Gate 阻断。完整新鲜 ≥200 轮仍必须在 d.2 目标环境完成。
