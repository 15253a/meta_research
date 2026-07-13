# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-13 ｜ 位置：步⑪ CP11.4c.3d.2 目标环境真实验收
- 检查点状态：CP11.4c.3c.1b 功能 `e1654fa` 与 build_log 0089 记账已闭合，定向/相邻/全量/静态验证通过；CP11.4c.3c 工具项完成，当前返回 CP11.4c.3d.2

## 正在做什么

- `ff00386` / `e792348` 已闭合普通研究在 claim boundary 后的永久关闭，并冻结 A high-water、exact source、
  explore view identities 与 confirmatory command。
- `e1654fa` 已实现 T1-only `run-confirmatory`、spent-before-spawn/recovery、canonical LODO output、guardian
  receipt、root-owned external audit authority、final-consumed v3 与 T1-D/score 双重准入；T2 明确拒绝 C/audit。
- firewall 同时在 B 后拒绝普通 explore remount，C 启动与 sandbox 准入均复验 exact explore-tree identity；
  任意 audit hash 或伪造 final marker 不能解锁 DREAMER。
- 外审指出导出的 `consume_final()` 曾只验 root audit JSON、未回放本地 C lifecycle；现已把完整
  spent/result/guardian/output/audit-input/root-authority 校验接到所有 firewall admission 路径，并让 D 再验
  terminal drained guardian receipt。审计 input 会先派生并检查 256 KiB authority 上限，再发布任何 durable copy。
- 第 2 轮外审指出：旧/preseeded `confirmatory.json` 可借同内容 destination 被接受、错误
  authority path 会先留 research copy、failed audit 的 work 内 ref 可被 research UID 删除后替换。现已：
  C 首次 spend 前拒绝整个预置 run namespace，success 必须精确绑定 sandbox `promoted.json`
  ledger；audit 先完成路径/冲突/大小预检；sealed truth 旁新增 root-owned `0555/0444`
  immutable decision ledger，以 canonical work-root 为唯一 verdict key。
- 第 2 轮后自审又关闭了一个 crash window：immutable decision 现先于 external authority 落盘；
  若中途崩溃，只能 exact retry 修复链，不能换路径/换 verdict。runbook 也把 authority 目录改为
  root-owned `0711`，确保真实跨 UID 的 D 能按已知路径读 `0444` authority，同时不可列目录/写入。
- 本检查点按 `ROADMAP.md` CP11.4c.3c.1b 补最薄的一次性 C 生命周期，不增加第二 DB/daemon/scheduler；
  最终真实验收按用户指定 `meta-researchv2/fixed_and_test` 规则由用户参与。

## 工作区状态

- 功能 checkpoint `e1654fa` 与紧随的 build_log 0089/ROADMAP/INDEX/本记录记账提交成对闭合；无其他已知工作区改动。
- 施工约束、`fixed_and_test` README/模板、qualification runbook、ROADMAP 缺口已实读。
- 第一轮前 qualification 全集 **134 passed**，第一轮修复后 **136 passed**。第二轮修复+自审后：
  firewall/runner **52 passed**，qualification data/firewall/metrics/runner **142 passed**；相邻
  sandbox/guardian/reconcile/run/deployment **148 passed**。新回归含 preseeded namespace、promotion 缺失/删除、
  conflicting authority 零本地残留、decision-first crash repair、failed verdict 不可替换、D 缺 root ledger 拒绝。
  按 README 从 `meta-research/` 启动的唯一有效全量为 **1818 passed, 1 skipped, 0 failed**；
  runbook 9 JSON + 16 shell blocks、62 fences、五路 CLI help、compileall 与 `git diff --check` 均通过。

## 下一步动作

1. CP11.4c.3d.2 的目标环境就位后，先与用户冻结预期，再跑真实 ≥200 轮、故障注入、T1/T2 和最终一次全量。

## 关键坑

- C 的 machine success 只证明冻结身份、单次执行和 schema；LODO 隔离、指标/统计、controls 与 novelty 由
  root evaluator 显式审核，不能把 operator assertion 冒充机器重算。
- C 必须 spent-before-spawn、exact frozen source/views、排除 DREAMER、失败不可重跑；D 不能仅凭自报指标放行。
- 当前 host 虽有 8×A100，但 Docker `cgroup_driver=none` 且无 NVIDIA runtime；只适合定向自验，不适合最终长跑。
- `fixed_and_test` 的人工验收不能由模型代签；building 先产机器终验矩阵和原始 evidence，真实运行预期须在运行前与用户冻结。
