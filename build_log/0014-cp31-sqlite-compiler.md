# 0014 · CP3.1 SqliteCompiler（M2：DB→确定性四区 context_pack）

- date: 2026-07-08
- commit: 2110f02 — feat: CP3.1 SqliteCompiler（M2 确定性四区包）
- branch: main
- 检查点 / 步: CP3.1（属：步③ M2 上下文编译器 · 首检查点）

## 决策
M2 首检查点：新增 `orchestrator/compiler_sqlite.py`（SqliteCompiler），**与 M0 StubCompiler 并存不替换**
（M0 driver 仍用 Stub、基线绿；M3 Advancer 接真组件）。DB 真相 → 每阶段**确定性四区** context_pack。
- **核心验收（§7.1 M2）字节一致（diff=0）**：同快照+配方(policy)+预算+target → pack_hash + 四区字节完全一致。
  一切遍历 ORDER BY 唯一键定序、无 wall-clock/随机/dict 无序、json sort_keys、budget float 统一、pack_hash \x00 分隔。
- **applicability 徽标（编译器确定性规则，§4.5.1）**：已关闭结论处 join 该 answer 当前 goal_ver 的 answer_applicability 行、
  渲染单行六枚举徽标（needs_revalidation/contradicted 附回看题 QN(状态)）；无行=无徽标、不占额度。
- 四区：①固定锚(不截断) ②结构邻域(祖先链) ③检索区(recall=CP3.2 占位) ④引用区(CP3.2 占位)。观测摘要段+status_card=CP3.3。

裁量：编译器用**普通只读连接**（可读 execution_observation 渲观测摘要给 reasoning——gate 判据禁读由 SqliteGate
authorizer 另管、二者分离，§3.1.2）；bundle 完整计划切片（build_target 行）延至 M3（plan gate 产 build_target 后），
本轮 target_id 已消费+占位；嵌入 defer（无模型）。

## 改动文件
- `orchestrator/compiler_sqlite.py` — 新增：SqliteCompiler（render 四区 + manifest 溯源 + applicability 徽标 + 祖先链 + 开放集含本轮 Qn）。
- `orchestrator/__init__.py` — 修改：模块地图加 compiler_sqlite。
- `tests/test_compiler_sqlite.py` — 新增（14）：四阶段字节一致 + 四区结构 + applicability 六枚举徽标（含 needs_revalidation→QN）+ 无行无徽标 + manifest 纯函数回归 + bundle target_id 消费 + 开放集排除 pending dep + 祖先链。

## Review（codex-chatgpt gpt-5.5/xhigh + 内审 Opus 子代理）
- **内审（Opus，带实测探针）**：REQUEST_CHANGES——确定性 headline **实测过硬**（跨文件连接 + 跨插入序同逻辑态皆字节一致）。**1 BLOCKER**：manifest(pack) 的 sources 取自实例态 _last_sources 而非 pack → 中间穿插 render 会串错来源（P6 溯源路径，被测试掩盖）。**2 SHOULD**：bundle 无 target/plan 内容、target_id 死参；_open_set 丢了 M0 依赖的「含本轮 active Qn」。**NIT**：closed 未按 goal_ver / pack_hash 无分隔 / budget 返 int。**全部采纳**（manifest 改按 pack_hash 取的纯函数 + 回归；bundle 分支消费 target_id + 占位注；_open_set 复原 active Qn；注释 + \x00 分隔 + float）。
- **codex 第 1 轮**：REQUEST_CHANGES——**2 BLOCKER**：① render 未钉单一读快照（多 SELECT 间并发提交致混态包，破「同快照」）② _open_set 不限 goal_id（泄漏别 goal 可调度问题）。**2 SHOULD**：pack_hash 漏第四区 refs；manifest 仍实例态依赖（跨实例/重启返 []）。**2 NIT**：bundle target_id=None 静默欠定；contradicted→qN 与六枚举形态不符。**全部采纳**（render 裹单读事务；_open_set 加 goal_id；hash 纳入 refs；sources 落 ContextPack 使 manifest 纯函数；bundle fail-fast；徽标仅 needs_revalidation→QN）。
- **codex 第 2 轮**：APPROVE（无 BLOCKER；3 NIT 均**前瞻性**、不阻塞 CP3.1：CP3.2 接 retrieval/refs 时须放同一读事务；refs 规范化形式待 CP3.2 定；__init__ 只读为架构约定、未强制 mode=ro）。
- 未采纳意见及理由：无（NIT 前瞻项转 CP3.2 遗留）。

## 验证
- 命令：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`
- 关键输出：
  ```
  281 passed
  ```
  （M0/M1 基线 267 + 新增 14 compiler）。核心：test_render_byte_identical 四阶段连渲两次 pack_hash+四区字节全等。
- 步级验证：本检查点未收尾步③（M2 还差 CP3.2 recall/O(1) + CP3.3 观测/status_card）。
- 结论：**通过**。

## 遗留 / 回退
- 待办：CP3.2（Recall 四级 + 复用判定 O(1) selector §4.1.5 + EXPLAIN 证明 + Ctx）；CP3.3（观测摘要段 + status_card + authorizer 负例 + import deferred 不产 target 断言）。
- **CP3.2 必接（codex 第2轮前瞻）**：① retrieval/refs 的 DB recall/ref 读取须放进 render 现有的同一 `BEGIN…COMMIT` 读事务（护同快照）；② refs 规范化形式明确（若 Ref 变 dataclass/对象则 asdict+sort_keys，防不可序列化/字段序影响确定性——现 refs 空，pack_hash 已把 json.dumps(refs) 纳入以定口径）。
- 悬案：bundle 完整计划切片 = M3（plan gate 产 build_target 后）；嵌入语义召回 = 有模型时；编译器只读连接 mode=ro 由 M3 调用方传入。
- 回退：`git revert 2110f02`（compiler_sqlite + tests 新增，interfaces.py 加兼容字段、__init__ 注释改，无对既有模块行为改动，回退安全）。
