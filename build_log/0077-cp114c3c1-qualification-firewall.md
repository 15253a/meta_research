# 0077 · CP11.4c.3c.1 T1/T2 qualification firewall

- date: 2026-07-12
- commit: `2a98e4ba1b97d245e806a16700bd8ead8c1b1c48` — feat: add CP11.4c.3c.1 qualification firewall
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3c.1（属：步⑪ CP11.4c.3c 薄验收与科学隔离工具）

## 决策

在现有 work-root、instance lease、Docker sandbox、execution guardian 与 receipt 上增加薄 qualification 边界；
不增加第二数据库、daemon、scheduler 或研究状态机。T1/T2 都先冻结 canonical contract 与 claim，研究 worker
禁用 host/external tools、repo import、asset refs 和非精确只读 mount。DREAMER 在 final 前不可见；SEED 的 15 个
final fold 也全部在 final 前不可见，避免同一持久研究进程跨 fold 累积 source labels 后恢复 target labels。

最终源码树与 runtime identity 冻结后先原子消费一次 final capability，再在 spawn 前逐 unit 写 spent receipt。
T1 只执行一个 DREAMER X-only unit；T2 在冻结 predictor 内执行 3 seeds × 15 LOSO folds，且每次容器只挂一个
fold。失败或不确定 unit 不重跑，只能从 exact guardian receipt 恢复已完成输出。root scorer 忽略 candidate stdout，
从 sealed truth 与 canonical probabilities 独立重算；已有 score replay 也必须重新读取输入并重算后才能接受。

GPU qualification 复用 deployment canary 的同一套日志、inventory、guardian receipt、lease fence 与 sandbox spec
核验，并把 candidate 绑定到 claim/source/runtime/contract/guardian owner；不复制第二份 GPU 证据协议。

## 改动文件

- `orchestrator/qualification_data.py` — 新增真实 SEED/DREAMER ZIP 适配、opaque ID/shuffle、public X-only views、
  root-only sealed truth、canonical per-view receipt 与跨 UID 权限发布。
- `qualification_firewall.py` — 新增 immutable contract/claim/final marker、exact view ledger、T1/T2 mount policy 与
  VEPFS no-clobber receipt publication/recovery。
- `qualification_runner.py`、`qualification_metrics.py` — 新增 one-shot/spent-before-spawn final runner、恢复、
  canonical prediction 闸、独立 accuracy/balanced-accuracy/macro-F1/NLL/ECE scorer 与 score replay 重算。
- `execution_sandbox.py`、`deployment_preflight.py`、`harness.py` — 复用 verified firewall、共享完整 GPU canary
  authority validator，并补 completed/non-exit guardian recovery。
- `run.py`、`runner.py`、`attack_stages.py`、`dependency_image_runtime.py` — 主入口在任何 Docker/SQLite/connector
  前加载 firewall；qualification worker 禁 host/external tools，拒 custom runner、repo import、asset refs，
  derived dependency sandbox 继承同一 firewall。
- `QUALIFICATION.md`、`README.md`、`requirements-dev.txt` — 新增固定 runbook、诚实边界及 NumPy/SciPy 依赖。
- `tests/test_qualification_*.py` 及相关 runner/sandbox/attack 回归 — 覆盖真实格式 synthetic fixture、跨 UID、
  pre-final 拒挂、45-cell one-shot、spent/recovery、GPU preseed、score tamper、tool-free 与入口 fail-closed。

## Review

- 内部 data ABI、core firewall、final runner 三路只读复审最终均未发现 BLOCKER；确认真实 archive → view →
  claim → one-shot unit → root scorer 闭环，T2 pre-final 全 fold 拒绝，GPU exact authority 与 score replay 重算。
- 外审第 1 轮：`codexro-review` 的 ChatGPT token 已失效，HTTP 401，无代码意见或 verdict。
- 外审最终第 2 轮：按约定从 root 刷新只读 reviewer 副本，但 root 副本自身也是失效 API key，再次 HTTP 401，
  无代码意见或 verdict。已到两轮上限，未发第 3 轮。

## 验证

- qualification data/firewall/metrics/runner + deployment preflight 相关组：`132 passed`；唯一失败为内部私有方法
  测试替身不接受新增 keyword，恢复原签名后定向 `8 passed`。
- 最终 GPU marker-time/owner binding 与 score replay 变化定向：`3 passed`；publication ancestor/cross-UID：
  `2 passed`。
- execution sandbox、observation recovery、runner usage、attack advance 受影响回归：`164 passed in 202.53s`。
- 真实 DREAMER 433 MiB archive：324 个 scored records、128 Hz、类别 161/163；non-root contract/claim/verify
  通过。真实 SEED 9.7 GiB archive：15 folds × 10,182 opaque IDs、三类齐全；public hardlink 去重与 non-root
  T2 contract/claim/verify 通过。临时数据与 secret 已删除。
- 相关 `py_compile`、`git diff --check`、staged diff check 均通过。未跑全量：遵守用户“实现期只跑相关验证，
  最后检查点只做一次全量”的要求。

## 遗留 / 回退

- 本检查点提供可运行的机械输入防火墙，不证明 novelty、统计优越性或 operator 手工提供 bytes 的来源；
  operator 与最终源码选择仍是显式信任边界。
- 当前节点没有 NVIDIA container runtime，GPU 正向 qualification 尚未执行；目标 VEPFS 两节点 lease/fd/WAL
  canary、预声明 fault soak、evidence pack 与真实 ≥200 轮仍分别属于 CP11.4c.3c.2/.3 和 CP11.4c.3d。
- 无 DDL。代码回退：`git revert 2a98e4ba1b97d245e806a16700bd8ead8c1b1c48`。若已安装 contract 或消费 final，
  不要用不识别 qualification marker 的旧代码继续该 work-root；应归档/隔离后在新 work-root 回退运行。
