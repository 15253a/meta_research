# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-09 ｜ 位置：步⑪ CP11.1 外部产物接纳硬化
- 检查点状态：代码已构建，待隔离 staged diff + 外审 + 提交

## 正在做什么

CP10.2 已以 `03d3ffd` 提交并记账。当前工作树已有 CP11.1：严格解析 metric，所有外部前缀 ID
在转 int 前限 SQLite 边界，reasoning 语义拒收落 durable terminate，且区分 Gate 业务拒与 DB 不变量损坏。
下一步只暂存 CP11.1 的 6 个文件，不得混入已完成的 CP11.2 控制面工作树改动。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- CP10.2：`03d3ffd` + build_log 0047；最终全量 `754 passed`。
- CP11.1 修改文件：`ids.py` / `gate_sqlite.py` / `statestore_sqlite.py` / `attack_stages.py`
  / `test_attack_advance.py` / `test_gate_sqlite.py`。子代理定向 102 + 当时全量 746 已绿。
- CP11.2 已有未提交改动（console/notify/interaction/run/frontend/tests），严禁混入 CP11.1。

## 下一步动作

1. 在 ROADMAP 登记步⑪ / CP11.1，只 stage CP11.1 六文件。
2. 复跑 CP11.1 定向测试，对 staged diff 跑 codex 外审（最多两轮），提交。
3. 写 build_log 0048，再单独审查/提交 CP11.2 真实 confirm/reject + 文件 resolve/cancel 闭环。

## 关键上下文 / 坑

- `run.py` 只剩 CP11.2 未提交差异；CP11.1 不应 stage 它。
- CP11.1 重点反例：5000 位 metric/ID 不能先 `int()` 裸崩；未知 DB IntegrityError 不能洗成业务 GateReject；
  answer 与 tree/selection 按「独立单调关问 + 后续原子批」分段接纳。
- 每个检查点必须：定向+全量测试 → staged diff codex 外审（最多两轮）→ 功能提交 → 独立 build_log 提交。
