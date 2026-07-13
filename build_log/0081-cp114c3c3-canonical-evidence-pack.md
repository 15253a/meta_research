# 0081 · CP11.4c.3c.3 canonical evidence pack 与离线续跑证明

- date: 2026-07-13
- commit: `6c05666c14d7a1691ac422ba14a609538785f2b8` — `feat: add CP11.4c.3c.3 canonical evidence pack`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3c.3（属：步⑪ CP11.4c.3 最终生产验收工具）

## 决策

增加一个薄的 owner-only、非压缩、内容寻址 evidence packer/offline verifier，不新增
restore engine、DB、daemon、scheduler、SSH orchestration 或通用 workflow。恢复仍只走既有
`storage_ops restore-with-import-materializations`，续跑仍只走既有 `orchestrator.run`。

v1 布局固定为：

```text
<canonical-manifest-sha256>.evidence/
  manifest.json
  READY.json
  objects/sha256/<digest>
```

manifest 只保留 protocol/source root/可选 resume protocol 与排序后的
`kind + logical_id + sha256 + bytes`；verifier 从包内事实重建结论，不信任 manifest 中的自述
claim。公开 verify 首先以 no-follow directory fd 锚定 pack root，后续只经
`/proc/self/fd/<rootfd>` 读取，最后再对 root/目录/文件的 dev/inode/ctime/mode/owner/link
做 identity seal；拒绝 symlink/hardlink、未知根条目、缺失/多余 object、bytes/hash 漂移和超限。

恢复证明只接受 exact one-cycle：target adoption cN 的 backup 与 source cN 相同，
`restore.json` 精确绑定 source root/cycle/manifest/backup，target 无 restore marker/parent claim，且 cN+1
恰好是有 route 的 done research cycle，存在同轮 success research runner_call 及其 ledger。它不把
exit code、fault receipt 或 hash-only copy 推断成恢复成功。

## 闭包与诚实边界

- SQLite backup 继续走既有 quick/FK/schema/terminal 深验；asset inventory 与包内 DB 精确对账。
- execution-log mirror 对每个 DB 登记项复验 index、有界 deterministic single-member gzip 及 raw
  hash/bytes，并拒绝未登记 object。
- repository roots/target ids 从包内 DB `plan_ref` 重建；完整 published ledger 仍绑 receipt 和 tree，
  DB hash 则通过生产同款 `spec_ledger` 投影计算，允许合法 Git/LFS provenance 字段。
- dependency capability 从 DB-bound execution-image 重建；lock 允许安全嵌套 source path，但 object 内按
  固定 basename 取文件。pack 只收 receipt-bound lock/wheels/installed+context/runtime+check/image
  语义文件；install/build/save logs、process pointers、`build/image.id` 与动态 sandbox metadata
  未被 provider receipt 内容绑定，故不冒充恢复闭包。
- 始终输出 `real_codex_resume_verified=false`、`qualification_receipts_verified=false`、
  `full_restore_verified=false`。checkpoint/log 正本、路径 relocation、完整 work-root DR 与来源签名仍不在 v1
  claim 内；manifest hash 须外存到变更单/不可变审计系统。

## 改动文件

- `meta-research/orchestrator/evidence_pack.py` — pack/verify CLI、流式 CAS 复制与限额、root-fd 锚定、
  storage/log/import/dependency/fault/qualification/canary 域核验和 exact-one-cycle resume proof。
- `meta-research/tests/test_evidence_pack.py` — 真 restore+续跑组合、缺失/篡改/重写、TOCTOU、
  限额、不诚实 claim、生产 repository ledger、nested lock 与真 builder diagnostics 正反例。
- `meta-research/README.md` — restore→one-cycle run→pack→offline verify runbook、输出字段及诚实限制。

## Review

- 内部设计/安全审查先后发现并修复：新旧 manifest schema 半迁移；pack root 父路径
  替换 TOCTOU；completion receipt 只做类型检查；asset inventory/repository roots 可脱离 DB；
  repository/dependency 文件只验“至少一个”；生产 full ledger 与 DB projection 哈希口径不同；
  真 builder 诊断文件会被 exact-set 误拒；nested lock source path 被错当作 object storage path。
- 外审第 1 轮：`codexro-review` 独立会话返回 HTTP 401，无 verdict。
- 外审第 2 轮（上限）：`codex-chatgpt` 给出 REQUEST_CHANGES：BLOCKER 是 import/dependency
  文件闭包不完整；两个 SHOULD 是 object 数量在构造大 set 后才限制、post-rename seal
  失败留下 final 目录；NIT 是长期依赖 storage 私有读接口。
- 两轮上限后本地修复 BLOCKER 与两个 SHOULD：以 DB/receipt 重建 exact file set，streaming
  计数超限立即失败，post-rename seal 失败尽力删除 final 并 fsync parent。依治理规则不发第 3 轮。
- NIT 未在本检查点推动大范围 storage API 稳定化；当前有回归锁定，但作为后续维护项保留。
- 修复生产正向兼容后，内部终审 APPROVE，确认 repository projection、semantic-only
  dependency pack 与 nested-lock 语义无剩余 BLOCKER。

## 验证

- `python -m py_compile orchestrator/evidence_pack.py` 通过。
- 生产形状定向用例：`1 passed in 2.76s`。同时覆盖 published ledger 额外 Git/LFS 字段、
  `deps/python-wheel-lock.json`、真 builder logs/process/sandbox extras 存在但不进入 pack。
- `pytest -q tests/test_evidence_pack.py ...`：`26 passed in 71.28s`。
- storage import + dependency inspector + repository offline inspector 相关回归：
  `22 passed in 37.36s`。
- `git diff --check` 与 staged check 通过；临时 pytest 目录已清理。
- 结论：相关验证通过。依用户要求，本中间检查点未跑仓库全量；全量只留最终生产验收。

## 遗留 / 回退

- 当前没有第二 GPFS/VEPFS 节点、NVIDIA container runtime、生产 connector/配额，因此未验收
  two-node、GPU、真 Codex ≥200 轮、fault 组合恢复、T1/T2 或最终全量。
- pack 不含完整 checkpoint/log 正本与任意路径重定位，不能替代完整 work-root DR。
- 内容 hash 不是签名；生产必须把 manifest hash 外存。
- verifier 仍依赖少量已测 storage 内部读接口；后续重构 storage 时应先提升为稳定只读 API。
- 回退：`git revert 6c05666c14d7a1691ac422ba14a609538785f2b8`。
