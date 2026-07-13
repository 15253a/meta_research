# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-13 ｜ 位置：步⑪ CP11.4c.3d.1.3 exact measurement reference
- 检查点状态：进行中；CP11.4c.3d.1.2 功能 `ad6a303` 已提交，真实 bundle 已完成

## 上一检查点结果

- 真实 full-attack smoke 的 c3 plan 连续把 synthetic seeds/sample/distribution 当成外部事实缺口，并输出
  三种不合法 `resource_request`；根因是 prompt 只给 schema 路径，且没有明确“前瞻设计决定”边界。
- plan 现在必须自行选择并锁定 synthetic protocol 参数，同时仍禁止虚构既有事实；system prompt 内联精确
  `resource_request` 骨架，StageProvider 在 bridge 前聚合校验错误并一次反馈，避免逐字段盲试。
- 从同一 c3 checkpoint 真实恢复后，plan 一次成功、plan review PASS，随后 bundle 生成、Docker build/run、
  code review 与 result review 均成功；storage verify 覆盖 c1-c3 snapshot。
- `tests/test_skills.py tests/test_stage_provider.py tests/test_schemas.py` **153 passed**；内部与外部只读审查
  均 APPROVE、无 blocker/major。依用户要求未跑仓库全量。

## 当前可用边界

- 默认 production 装配的 query/import adapter sideband 现在与 non-root preflight 契约兼容，且默认
  `/usr/local/bin/codex` 已有真实启动/应答证据，不再是“preflight 能启动、首次 query 必失败”。
- 单节点 development 已真实跑通 bootstrap、decompose、idea、plan/review、bundle、Docker execution 与双 review；
  修复没有增加第二 DB、daemon、scheduler、SSH orchestration 或通用 workflow。
- CP11.4c.3c 薄工具已闭合：qualification firewall、shared SQLite boundary、local/two-node
  canary 协议、fixed-linear fault runner 与 evidence pack 可组合使用。
- evidence v1 证明包内 bytes 闭包和一轮续跑，不是 restore engine，不声称完整 work-root DR、
  来源签名、真 Codex/GPU/two-node 或 qualification 已验收。
- 当前节点仍只有单机且无 NVIDIA container runtime；目标 GPFS 两节点正向、真实 ≥200 轮、
  T1/T2、故障注入组合验收和最终全量均未执行。

## 下一步动作

1. 完成 CP11.4c.3d.1.3：让 compiler 明示 evidence 只能引用 exact `mrN`，并移除 reasoning 对真实
   execution 的陈旧 “M0 fake” 定性；做定向测试和一次真实 reasoning 恢复/重跑。
2. 进入 CP11.4c.3d.2：在 dedicated VM/private cgroup + NVIDIA Docker + 目标 GPFS quota/second
   node/connector 到位后，按既有 runbook 执行真实 ≥200 轮。
3. 把 two-node canary、预声明 fault schedule、干净 restore+续跑 evidence pack 与 T1/T2 qualification
   作为同一生产验收批次，保存 manifest hash 到外部不可变审计记录。
4. 中间只跑相关验证；最终验收提交前只跑一次仓库全量，不反复制造长验证成本。

## 关键坑

- `reference/` 原始是单机 embedded SQLite WAL；两节点 VEPFS 是后续生产加固，不得倒称原始要求。
- evidence 目录 hash 是内容身份而非来源签名；没有外部保存 hash 时，同 UID 重写整包不能靠自证发现。
- `real_codex_resume_verified` / `qualification_receipts_verified` / `full_restore_verified` 仍必须为 false；
  不得用注入式 worker 的一轮续跑替代生产验收。
- 当前 host 能看到 8×A100-80GB，但 Docker 没有 NVIDIA runtime/cgroup/resource limit。清理本轮旧临时目录后
  pinned CPU image 已能完成真实 build/run；root overlay 仍处危险水位，pytest basetemp 必须放 VEPFS。
- compiler 当前把展示串 `mr1:1@1=...` 放进 reasoning 锚，模型会误把整串写入 evidence；Gate 只接受
  exact `mr1`。这是 d.1.3 的直接阻断点，不改 evidence/Gate 契约。
