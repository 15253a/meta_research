# 0073 · CP11.4c.3b.1 cycle snapshot spine

- date: 2026-07-12
- commit: `83ace54093a2f5654363a29958f05e4f11c0adb3` — feat: add CP11.4c.3b.1 cycle snapshot spine
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3b.1（属：步⑪ CP11.4c.3 最终生产验收）

## 决策

先建一条可用、可恢复、不扩系统的轮次快照主干：SQLite 仍是唯一结构化真相，每个终态 cycle
提交后同步顺序执行 online backup → 同备份 DB-derived views Git → CAS manifest → immutable pointer。
不引入后台 daemon、第二套 DB 或新调度器；restore/verify、retention、容量门和有界 GC 留给 b.2。

为了不在崩溃后伪造历史：

- 不可变 `genesis.json` 冻结 coverage 起点；新系统原生从 c1 开始，已有终态历史的旧库只对最新状态
  发布显式 adoption baseline。
- `pending/cN.json` 在 Git/pointer 前绑定 exact backup；重放时同一 fd 核 hash、SQLite schema、目标终态
  与 normal cycle 边界，拒绝改绑较早或较晚 DB 切面。
- pointer 始终最后发布；owner 丢失后 Git orphan 只能在 exact parent/message/tree 全一致时复用。
- research done 先完成 τ/global-stop 检查再快照；worker、failed、aborted 在各自终态边界快照。

## 改动

- 新增 `orchestrator/storage_governance.py`：SQLite 单文件 CAS backup、四份中文 views、独立 Git 父链、
  asset inventory/manifest/pointer、genesis/pending 补偿和完整链验证。
- `run.py` 在 connector/provider/Runner 开放前恢复 budget stop 并执行 startup reconcile。
- `SqliteAdvancer` 在同 System 重入、开新轮、worker 收口、轮后 stop、外层与格间 abort+pause 边界同步
  reconcile；失败向上抛，不重放已提交 Runner。
- README/ROADMAP 说清 b.1/b.2 分工、adoption、同 failure-domain 局限、实际资产盘点范围和
  registered 原件不 GC。

## Review

- 内部代码审查：逐次发现并修复 abort 边界、单轮 adoption、high-water 缺口、owner fence、
  open 窗口、runtime Git 漂移、O(N²) 验证、可执行 Git 信任、manifest TOCTOU、pending 语义、
  genesis 和 stage-boundary abort+pause；最终 `APPROVE`。
- 参考/文档审查：修复 startup stop 顺序、fresh/adoption 误判、views 中文、资产范围与
  failure-domain/GC 过度声称后 `APPROVE`。
- 外审第 1 轮 `REQUEST_CHANGES`：关于 Git orphan 父链与 pending 验证的 3 条与实际 exact
  parent/message/tree 代码及已通过的 c1 owner-loss 测试矛盾。本地复核确认无越过，并额外增加
  c2 orphan 复用与「同树+正确父链+错误 message」拒绝回归，均通过。
- 外审最终第 2 轮：完整 staged diff 内联提交后 CLI 等待 5 分钟仍无 verdict，且其工具调用被
  只读 sandbox/bubblewrap 拒绝；终止挂起进程，依两轮上限不再试。

## 验证

- 相关集：`test_storage_governance.py test_advancer.py test_run.py`，唯一新 `/tmp` basetemp，
  **105 passed in 46.11s**。其中 storage 子集 **18 passed in 9.75s**。
- `python -m py_compile` 与 `git diff --check` 通过。
- 未跑本检查点全量：开跑前 20G overlay 仅约 155MB 可用，上一次全量的 basetemp 已达 191MB 并将
  根盘打到 0B。按用户「中间只跑相关验证、最后才做全量」和单次检查点约束，不启动必然 ENOSPC 的全量。

## 遗留 / 回退

- b.1 已可在长跑中作为逐轮 recovery-point 主干，但存储治理整体尚未验收：b.2 仍须补
  离线 verify/restore、至少 3 代 retention、容量门、dry-run-first 有界 GC、raw-log 压缩镜像和
  import-materialization indexes/objects 可达闭包。
- 同 VEPFS failure domain 不是 fileset/跨站灾备；目标节点与二节点验收仍在 CP11.4c.3c/d。
- 无 DDL。回退：`git revert 83ace54093a2f5654363a29958f05e4f11c0adb3`。
