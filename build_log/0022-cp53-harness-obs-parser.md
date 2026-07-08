# 0022 · CP5.3 真执行 harness + 确定性观测 parser + suspect 真派生（OPEN #5 落地）

- date: 2026-07-08
- commit: 215c694 — feat: CP5.3 真执行 harness + 确定性观测 parser + suspect 真派生（M4；OPEN #5 落地）
- branch: main
- 检查点 / 步: CP5.3（属：步⑤ M4 真执行 + 真 log + import 物化）

## 决策
观测证据层换真（§4.3.1/§4.7/§3.1.2）。四件事：

1. **harness.py**：run_staged 跑**真子进程**（stdout+stderr 合流），staging 纪律 = 先 `.partial` 后**原子改名**
   （kill-9 只留 .partial、半成品不冒充完整 log；超时 kill+留 .partial+抛；同名旧 final 拒）。register_execution_log
   幂等入账（owner XOR）。**长操作零 DB**（§6.13：harness 只碰文件/进程，入账是其后的短事务）。
2. **obs_parser.py**：parse_log 纯函数（八机器事实字段；divergence 仅正 loss 域；非有限 loss→nan_seen；oom 词边界）；
   extraction_policy_hash（P6 复算锚）；ingest_observation（**content_hash 锚校验** + 幂等）；
   **suspect_for_attempt 三条口径**：①只认当前 (PARSER_VERSION, extraction_policy_hash) 观测行 ②有观测但无当前
   口径行 → **stale≠clean，fail-closed 返 1**（policy/parser 升级后旧行不得冒充当前口径干净；重 ingest 即恢复，
   log 字节在库零成本）③每 log 当前口径最新行、**跨 log OR**（任一 log 存疑即存疑）。全无观测→0（无据不疑；
   真管线 CP5.4 强制先 ingest 再注册）。register_parser_suspect_real 替 M2 恒 0 桩——**复用判定自此可对真执行
   上线**（build_log 0015 遗留闭）。
3. **gate_close_question 接真谓词**（§4.1.4「证据 attempt 被 parser 派生标存疑拒」）：SqliteGate 可选 parser_suspect
   callable（默认 None=M0–M3 行为），_gate_only_violation（写锁内）查。**观测隔离不破**：gate SQL 仍不可 SELECT
   观测表（authorizer 拒 9 表不变）；负向过滤豁免仅经该派生谓词（观测读走独立普通连接）——§3.1.2 钦定通道。
4. **OPEN #5 落地闭**：policy.yaml `observation` 节（parse + suspect 两组旋钮）+ policy.schema.json 对应封闭子
   schema。改节任一值=决策性变更（走评审）；旧观测行带旧 hash，复算按行内 hash 取当时口径。

## 改动文件
- `meta-research/orchestrator/harness.py` — 新增。
- `meta-research/orchestrator/obs_parser.py` — 新增。
- `meta-research/orchestrator/gate_sqlite.py` — 修改：构造加 parser_suspect + _gate_only_violation 存疑分支 + docstring。
- `meta-research/orchestrator/recall_sqlite.py` — 修改：桩 docstring 指向真谓词（真执行数据须用真）。
- `meta-research/policies/policy.yaml` — 修改：observation 节（决策性，OPEN #5）。
- `meta-research/schemas/policy.schema.json` — 修改：observation 子 schema + required + description（OPEN #5 已闭）。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图两行。
- `meta-research/tests/test_obs_parser.py` — 新增：20 测。

## Review
- **内审（Opus 子代理）**：REQUEST_CHANGES → 全修。**1 BLOCKER**（实证）：suspect 单取全局最新行——多 log attempt
  （train nan + 后入账干净 stderr 观测）掩盖 nan（安全关键谓词假阴性：坏测量可复用/可作证）→ 每 log 最新行、
  跨 log OR。1 SHOULD：divergence 乘法判式对首 loss≤0（log-likelihood 类）语义反转 → 正域守卫 + policy 注释。
  3 NIT（±inf 置 nan_seen / oom 词边界 / owner-XOR 前置）全修。另实证核可：幂等 IS ? 与部分唯一索引等价、
  staging 原子性、gate 隔离纪律、独立连接 WAL 读、hash 往返稳定、yaml↔schema 一致。
- **外审（codex-chatgpt gpt-5.5/xhigh）**：
  - 第1轮 REQUEST_CHANGES → 全修。**1 BLOCKER**：每 log 最新行未按 (parser_version, extraction_policy_hash)
    过滤——旧宽松 policy 行冒充当前口径干净（如 divergence_ratio 10→2 收紧后旧 flag=0 行未重 ingest 即当干净）
    → 当前口径过滤 + **stale≠clean fail-closed**。2 SHOULD：① ingest 不校验字节与 content_hash（调用方 bug 可
    用干净文本对脏 log id 产假干净观测）→ 签名改 log_bytes + 锚校验 ② wall_clock 非有限逃逸 → 归 None。
    1 NIT：旧 final 混淆 → 拒。
  - 第2轮 **APPROVE**（零 BLOCKER/SHOULD/NIT）：三段语义一致；「版本/policy 升级后全旧观测暂 suspect=1」
    保守取舍核可（只挡复用/作证、重 ingest 零成本）。
- 未采纳意见：无（两层三轮全采纳）。两层评审各抓一个**互补的假阴性向量**（内审：多 log 掩盖；外审：口径漂移
  stale）——安全谓词的纵深防御实证。

## 验证
- 命令：`pytest tests/ -q`
- 关键输出：
  ```
  409 passed in 46.01s
  ```
- 含真 subprocess 执行、超时留 .partial、复用被 suspect 挡（reuse_selector miss）、gate 拒存疑证据（GateReject）。
- 结论：通过。（CP5.3 未收尾步⑤；M4 步级验证在 CP5.6。）

## 遗留 / 回退
- 待办：CP5.4 attack advance 全链（管线强制「先 ingest 观测再注册」+ 注册段单事务组合）；CP5.5 import 物化；
  CP5.6 语义判据 5 判例收尾（判例⑤「log suspect 不成证据」的端到端断言已具雏形：test_real_suspect_blocks_*）。
- 回退：`git revert 215c694`（新模块+gate 可选参+policy 节；gate 默认 None 不改既有行为，回退不破基线绿）。
