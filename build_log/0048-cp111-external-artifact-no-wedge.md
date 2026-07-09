# 0048 · CP11.1 外部产物接纳硬化

- date: 2026-07-09
- commit: ac53516253d86def0ad4b512264a24965b26d1d9 — fix: CP11.1 阻断外部产物跨重启投毒
- branch: fix/architecture-hardening-20260709
- 检查点 / 步: CP11.1（属：步⑪ 生产硬化）

## 决策

消除「persist-then-consume 的外部坏产物在每次重启时确定性复读并裸崩」这类 poison-pill：

- `metric_value` 保留记录完整匹配，拒绝畸形、重复、NaN/Inf/溢出和 SQLite 越界 ID；
- 前缀 ID 在 `int()` 前用字符串长度/字典序限制 SQLite 正整数上界，5000 位输入只产生领域拒绝；
- reasoning 的 answer/evidence/tree 语义拒收落 `reasoning_rejected + terminate + done`，全新实例不再复读崩溃；
- tree/selection 批次先完整预检后写，SQLite 行与进程内 local-key 投影一起回滚；
- `GateReject` 仅表示已知业务门禁拒，未知 `IntegrityError` 升为 `GateInvariantError` fail loud。

answer 关问与后续 tree/selection 是明确两段：已经 I3 接纳的 answer 不因后续 tree 坏掉而反向删除；
tree/selection 批整体回滚并停机。该取舍已有回归锁定。

## 改动文件

- `ROADMAP.md` — 登记步⑪与 CP11.1–11.4 生产硬化路线。
- `meta-research/orchestrator/ids.py` — 新增 SQLite 正整数边界解析与宽容引用解码。
- `meta-research/orchestrator/gate_sqlite.py` — 引用统一 ID 解码，新增 `GateInvariantError` 区分未知 DB 损坏。
- `meta-research/orchestrator/statestore_sqlite.py` — 统一有界 ID，selection score 整批预检防半写。
- `meta-research/orchestrator/attack_stages.py` — metric 严格解析，reasoning 语义拒收持久收敛。
- `meta-research/tests/test_attack_advance.py` — 新增 metric/ID 边界、首次+重启 no-wedge、投影回滚和 answer 分段接纳回归。
- `meta-research/tests/test_gate_sqlite.py` — 新增未知 IntegrityError 不得洗成业务拒绝的反例。

## Review

- 内部复核第1轮：`REQUEST_CHANGES`。发现 5000 位 metric/ID 在边界判定前 `int()` 仍会裸崩、
  GateReject 分流过宽、answer/tree 事务语义不清与 score 半写缺口；全部修正并补回归。
- 外审（`codex-chatgpt` 模式 B，gpt-5.5/xhigh）第1轮：`APPROVE`，无 BLOCKER。
- 2 SHOULD 未作为本检查点 blocker：
  - I3 触发器错误仍用冻结 DDL 文案集合分类；当前 migration checksum 冻结，新 migration 时应改稳定错误码。
  - `apply_tree_ops` 仍以 `ValueError` 表示外部语义拒绝；当前实现契约与反面 DB 异常回归已锁定，后续可收敛专用异常类。
- NIT：前缀 ID 允许前导零是旧契约兼容取舍；metric 本身仍拒绝前导零。
- 证据：`/tmp/cp111-review-round1.md`。

## 验证

- `python -m pytest tests/test_attack_advance.py tests/test_gate_sqlite.py -q` → `72 passed in 41.53s`。
- `python -m pytest tests/test_statestore_sqlite.py tests/test_orchestrator.py -q` → `60 passed in 1.00s`。
- 当前合并工作树全量：`python -m pytest -q` → `754 passed in 157.61s`。
- `compileall` / `git diff --check` 通过。
- 步级验证：步⑪未收尾，CP11.2–11.4 继续。

## 遗留 / 回退

- 待办：CP11.2 真实人类控制闭环；CP11.3 状态/运行边界；CP11.4 调用回执、执行隔离与制品存储。
- 回退：`git revert ac53516`。
