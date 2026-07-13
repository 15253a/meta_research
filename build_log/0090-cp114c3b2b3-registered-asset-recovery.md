# 0090 · CP11.4c.3b.2b.3 registered asset recovery

- date: 2026-07-13
- functional commit: `866afda742a729ec61dcd96e849104b99715948f` — `feat(storage): close registered asset recovery`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3b.2b.3（闭合 CP11.4c.3b.2b、CP11.4c.3b.2 与存储治理建造项；不等于目标环境/跨站灾备验收）

## 结论

最新 retained SQLite high-water 中登记的 checkpoint 与 execution log 现在都有一次性离线副本、严格校验和
原路径 hydration 闭包。checkpoint 进入 raw SHA256 CAS + immutable per-row index，日志继续复用既有
deterministic gzip CAS；组合恢复按 SQLite → registered checkpoint/log → repository/dependency import CAS
顺序执行，在三者全部闭合前始终保留独立 continuation marker。

append-only DB 中的旧绝对 ref 不改写。`restore.json.registered_path_roots` 保存当前源及全部历史源根，运行时
checkpoint 消费和后续 mirror/restore 将旧 ref 映射到当前根；二次恢复会在 target/parent claim 发布前拒绝与
任一 lineage root 相等或互相嵌套。目标完成 receipt 不是自报：最终 marker release 会从源 DB + mirrors 重新构造
exact files authority，并逐文件复验 owner/type/link/mode/hash/bytes。

本项闭合的是 DB-registered checkpoint/log 与 DB 可达 import CAS，不是完整 work-root/fileset/站点灾备。
runner/guardian/sandbox/qualification authority、未登记失败产物、用户 uploads/input、views Git、connector cursor
仍需各自既有 authority 或目标环境独立故障域归档。真实两节点、≥200 轮、faults、T1/T2 与用户验收仍在
CP11.4c.3d.2。

## 决策与修改

- `storage_assets.py`：新增 checkpoint mirror layout、内容去重、原件/镜像 drift 检查、容量门和 replay-safe
  no-clobber hydration；把 checkpoint、execution log 合成 canonical registered authority。恢复文件固定为
  owner-only `0400` 单链接常规文件，completion receipt 最后发布并自验。`state/`、`views/` 与 SQLite/restore/
  lease 控制名明确保留，不能被 DB ref 当作 hydration 目标。
- `storage_restore_contract.py` / `storage_imports.py`：新增 registered continuation protocol。import-only marker
  不能冒充 registered 恢复；registered marker 只有在 exact source authority、target receipt、所有 hydrated 文件和
  import CAS 都通过后才解除。copy/decompress 失败立即清理本次私有 temp，marker 继续 fail-closed。
- `storage_paths.py` / `storage_ops.py`：严格读取 canonical owner-only restore receipt，保留多跳 path lineage，
  解析旧绝对/相对 ref；所有 restore 在发布前检查 target 与完整 lineage 不相等、不互嵌。SQLite receipt 明示
  `registered_path_roots` 与 continuation mode。
- `attack_stages.py`：eval-only 与 run eval 的 checkpoint 消费统一先走 lineage resolver，随后仍由 artifact
  capability/fd binding 核 hash 和路径身份，不把普通 host path 直接交给 child。
- `evidence_pack.py`：manifest 冻结 `source_registered_path_roots`，offline log-mirror verifier 用它重建旧 ref 的
  当前相对 identity，并拒绝非 canonical、重复、嵌套或歧义 roots。registered 完整恢复 target 可参与 v1 的
  SQLite/import-CAS exact-one-cycle probe，但 pack 故意不收 registered completion，不升级为 registered hydration
  或 full-restore 证明；resume receipt roots 必须与 source manifest exact equality。
- `storage_ops.py` / `README.md`：增加 mirror/verify/standalone replay/完整组合 restore CLI 与 runbook，明确
  latest-high-water、source 必须保持停止、scope 和外部故障域边界。

## review

- 第 1 轮 `/tmp/codexrev.YoAVAK/verdict.md`：`REQUEST_CHANGES`。BLOCKER 是 A→B 后可把二次 target 放进
  历史 A 子树，发布后自身 lineage resolver 会拒绝；两个 SHOULD 是 import-only marker 可跳过 registered
  final recheck、evidence-pack offline log verifier 未跟随 lineage；NIT 是 mirror copy 的 `os.open` 裸抛。
  四项均先补失败测试再修。因最初只按进程名检查，曾误启动 `/tmp/codexrev.Nesz52` 的重复只读审查，发现原进程
  仍在后立即终止且未产 verdict，不计有效轮次。
- 第 2（最终）轮 `/tmp/codexrev.PxOIJb/verdict.md`：`APPROVE`，无 BLOCKER。三个 SHOULD 是 registered target
  尚不能参与窄 resume probe、offline resume roots 未 exact 绑定 source manifest、hydration 未保留全部可写控制面
  namespace；NIT 是 copy 失败 temp 留到下次 replay。按两轮上限不再发第 3 轮，四项全部成立并以失败测试闭合。
- 只读 reviewer 账号能完成 `diff --check`、compile/import，但其 PATH/系统 Python 没有项目 pytest；本检查点的
  pytest 结果由主施工环境产生并在下节记录。

## 验证

- registered/storage ops/import：`63 passed in 30.00s`。
- evidence pack（VEPFS basetemp）：`30 passed in 84.70s`。
- attack checkpoint 消费相邻回归：`80 passed in 44.24s`。
- storage governance/run/instance lease：`118 passed in 33.94s`。
- 按 `meta-research/` 全量、VEPFS basetemp：`1841 passed, 1 skipped in 1558.01s`，零 failure/error。
- `python -m compileall -q orchestrator tests`、`python -m orchestrator.storage_ops --help`、
  `git diff --cached --check` 全部返回 0。

## 遗留与回退

- 当前 host 不是目标生产环境：虽然宿主可见 GPU，但 Docker 使用无资源隔离的 rootless/proxy 路径，缺 NVIDIA
  runtime、private cgroup、权威 GPFS byte+inode quota、第二节点和生产 connector。这里的全量绿不能替代目标矩阵。
- mirror/restore 依赖 source work-root 内 storage subtree 存活且 source owner 停止；整 fileset/站点丢失必须由目标
  环境提供独立故障域归档并另做演练。registered files 不进入本项目 GC。
- evidence-pack v1 只证明其声明的 SQLite/import-CAS resume 切面；最终 building 证据还须单独保存本 CLI 的 raw
  mirror/verify/restore outputs 与 completion receipt，不能用 `full_restore_verified=false` 的 pack 代签。
- 功能回退：`git revert 866afda742a729ec61dcd96e849104b99715948f`。已发布的 immutable mirrors、indexes、
  restore receipts 和 hydrated files 是审计制品；回退代码不等于删除或降级它们，清理须另经显式运维决策。
