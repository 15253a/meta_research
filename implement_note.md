# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-12 ｜ 位置：步⑪ CP11.4c.3c.2 VEPFS 两节点 canary / fault soak（下一检查点）
- 检查点状态：空闲；CP11.4c.3c.1 功能 `2a98e4b` 已提交，记账完成

## 上一检查点结果

- 已用真实 SEED/DREAMER archive 制备 canonical public X-only / root-sealed truth views，并冻结 exact
  view ledger、contract 与 claim；T1 DREAMER 和 T2 全部 final folds 在 final 前均不可挂载。
- final source/runtime 冻结后一次性消费 capability，T1 1 unit、T2 3×15 units 均 spent-before-spawn；
  candidate 只交 canonical probabilities，root scorer 独立重算，score replay 不直接信任已有 metrics。
- qualification 复用现有 sandbox/guardian/lease/GPU canary，不增加数据库、daemon、scheduler 或研究状态机；
  host tools、custom runner、repo import、asset refs 与 extra mounts 全部 fail-closed。
- 相关验证：qualification/deployment **132 + affected 13 passed**，sandbox/entry **164 passed**；真实
  DREAMER 324 records、SEED 15×10,182 IDs 与跨 UID contract/claim/verify 通过；未跑全量。
- 内部三路无 BLOCKER；外审两轮均因独立凭证 HTTP 401 无 verdict，已到上限。

## 当前可用边界

- CP11.4c.3c.1 已达到可用的 CPU T1/T2 机械隔离与独立出分级别；operator/source provenance、novelty 与
  统计优越性仍不由该工具自动证明。
- 当前节点无 NVIDIA container runtime，GPU 只有 exact fail-closed 负向证据，尚无正向 qualification。
- 目标 VEPFS 两节点 fault canary、canonical evidence pack、真实 ≥200 轮与最终唯一一次全量仍未完成。

## 下一步动作

1. 只做 CP11.4c.3c.2：在现有 lease/guardian/storage_ops 上加薄的两节点 canary 与预声明 fault schedule；
   不加常驻服务、第二 DB、通用 workflow engine 或新状态机。
2. 优先验证 lease takeover、fd identity、SQLite WAL/restore continuation 与已有 failure receipts；中间只跑
   canary/storage/lease 相关测试，全量继续留到最终检查点。
3. 若当前环境拿不到第二节点，先把可执行 runner/receipt 做到 fail-closed，并如实保留外部环境阻塞，
   不把单节点模拟写成两节点通过。
